# -*- coding: utf-8 -*-
"""
Проверка миграции v1.7 → 2.0 (этап 1) на тестовой БД, созданной по схеме v1.7,
и аккаунтов/ролей/сессий (этап 2).
Запуск из корня репозитория:  python tests/test_migration.py
В образ не попадает (Dockerfile копирует только app.py, templates/, static/).
"""
import glob
import hashlib
import importlib.util
import itertools
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import time
from datetime import date
from pathlib import Path

from werkzeug.security import check_password_hash

ROOT = Path(__file__).resolve().parent.parent
time.sleep = lambda *_: None  # не ждём задержек при неверных паролях
_counter = itertools.count()
FAILED = []


def check(cond, msg):
    print(("  ✓ " if cond else "  ✗ ") + msg)
    if not cond:
        FAILED.append(msg)


def load_app(cfgdir, **env):
    """Импортирует app.py «с нуля» с нужным окружением (миграция выполняется при импорте)."""
    for k in ("ADMIN_PASSWORD", "RESET_ADMIN_PASSWORD", "CONFIG_DIR"):
        os.environ.pop(k, None)
    os.environ["CONFIG_DIR"] = cfgdir
    os.environ.update(env)
    name = f"app_under_test_{next(_counter)}"
    spec = importlib.util.spec_from_file_location(name, ROOT / "app.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


# ───────────────────────── тестовая БД по схеме v1.7 ─────────────────────────
TASKS_NEW = [
    {"id": "homework", "name": "Домашка до 18:00", "amount": 100, "once_per_day": True},
    {"id": "dishes", "name": "Посудомойка", "amount": 50, "once_per_day": False},
    {"id": "trash", "name": "Мусор", "amount": 50, "once_per_day": False},
    {"id": "t1a2b3", "name": "Уборка комнаты", "amount": 70, "once_per_day": False},
]
GRADE = {"10": 200, "9": 200, "8": 100, "7": 0, "6": 0, "5": 0, "4": 0, "3": -100, "2": -100, "1": -100}
CONTROL = {"10": 1000, "9": 600, "8": 300, "7": 0, "6": 0, "5": 0, "4": 0, "3": -300, "2": -300, "1": -300}
CONTRACT = {"number": "1/2026", "city": "Тверь", "date": "2026-09-08", "end": "2027-05-31",
            "student_full": "Иванов Пётр Сергеевич", "student_age": 12, "student_class": 6,
            "parents": [{"name": "Иванов Сергей", "role": "Папа", "female": False},
                        {"name": "Иванова Анна", "role": "Мама", "female": True}]}

# (id, date, kind, descr, grade, amount, payout_id)
EVENTS = [
    (1, "2026-09-08", "homework", "Домашка до 18:00", None, 100, 1),
    (2, "2026-09-08", "dishes", "Посудомойка", None, 50, 1),
    (3, "2026-09-09", "grade", "Оценка: 9", 9, 200, 1),
    (4, "2026-09-12", "walk", "Прогулка с собакой", None, 30, 2),       # категории «walk» нет в тарифах
    (5, "2026-09-14", "control", "Контрольная: 10", 10, 1000, 2),
    (6, "2026-09-18", "carry", "Остаток после частичной выплаты", None, 30, None),
    (7, "2026-09-19", "trash", "Мусор", None, 50, None),
    (8, "2026-09-19", "custom", "Штраф за телефон", None, -100, None),
    (9, "2026-09-20", "t1a2b3", "Уборка комнаты", None, 70, None),
    (10, "2026-09-21", "walk", "Прогулка с собакой", None, 30, None),
]
PAYOUTS = [(1, "2026-09-11", 350), (2, "2026-09-18", 1000)]  # №2 — частичная (1030 → 1000), остаток в carry


def build_v17(cfgdir, tariffs):
    """tariffs: dict -> tariffs.json; None — файла нет."""
    con = sqlite3.connect(os.path.join(cfgdir, "uchet.db"), isolation_level=None)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("""CREATE TABLE IF NOT EXISTS payouts(
        id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT NOT NULL, amount INTEGER NOT NULL)""")
    con.execute("""CREATE TABLE IF NOT EXISTS events(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT NOT NULL, kind TEXT NOT NULL, descr TEXT NOT NULL,
        grade INTEGER, amount INTEGER NOT NULL,
        payout_id INTEGER REFERENCES payouts(id))""")
    con.executemany("INSERT INTO payouts VALUES(?,?,?)", PAYOUTS)
    con.executemany("INSERT INTO events VALUES(?,?,?,?,?,?,?)", EVENTS)
    con.close()
    if tariffs is not None:
        with open(os.path.join(cfgdir, "tariffs.json"), "w", encoding="utf-8") as f:
            json.dump(tariffs, f, ensure_ascii=False, indent=2)


def q(cfgdir, sql, args=()):
    con = sqlite3.connect(os.path.join(cfgdir, "uchet.db"))
    con.row_factory = sqlite3.Row
    try:
        return con.execute(sql, args).fetchall()
    finally:
        con.close()


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


# ───────────────────────── сценарии ─────────────────────────
def scenario_a():
    print("\n[A] v1.7 с tariffs.json нового формата, частичная выплата + carry, удалённая категория")
    d = tempfile.mkdtemp()
    build_v17(d, {"student": "Пётр", "tasks": TASKS_NEW, "extra_payout_limit": 800, "contract": CONTRACT,
                  "grade": GRADE, "control": CONTROL})
    tj = sha(os.path.join(d, "tariffs.json"))
    mod = load_app(d, ADMIN_PASSWORD="pw1")

    check(len(glob.glob(os.path.join(d, "uchet.db.bak-v1.7-*"))) == 1, "создана одна резервная копия")
    bak = glob.glob(os.path.join(d, "uchet.db.bak-v1.7-*"))[0]
    bcon = sqlite3.connect(bak)
    check(bcon.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 10
          and "kind" in [r[1] for r in bcon.execute("PRAGMA table_info(events)")],
          "копия содержит исходную структуру v1.7 и все 10 записей")
    bcon.close()
    check(q(d, "SELECT value FROM meta WHERE key='schema_version'")[0][0] == "1", "meta.schema_version = 1")
    check(q(d, "SELECT name FROM sqlite_master WHERE name IN ('events_v17','payouts_v17')") == [],
          "временные таблицы v1.7 удалены")
    ch = q(d, "SELECT * FROM children")
    check(len(ch) == 1 and ch[0]["name"] == "Пётр" and ch[0]["extra_payout_limit"] == 800
          and ch[0]["grade_min"] == 1 and ch[0]["grade_max"] == 10, "создан один ребёнок с именем/лимитом/шкалой")
    cid = ch[0]["id"]

    tasks = {r["name"]: r for r in q(d, "SELECT * FROM tasks")}
    check([t for t in tasks if not tasks[t]["is_deleted"]] ==
          ["Домашка до 18:00", "Посудомойка", "Мусор", "Уборка комнаты"], "4 активные категории в прежнем порядке")
    check(tasks["Домашка до 18:00"]["once_per_day"] == 1 and tasks["Посудомойка"]["once_per_day"] == 0,
          "флаг «раз в день» перенесён")
    check(tasks["Прогулка с собакой"]["is_deleted"] == 1, "«walk» восстановлена как удалённая, название из событий")

    ev = q(d, "SELECT * FROM events ORDER BY id")
    types = [e["type"] for e in ev]
    check(types == ["task", "task", "grade", "task", "control", "carry", "task", "custom", "task", "task"],
          "kind → type разобран верно: " + ",".join(types))
    check(all((e["task_id"] is not None) == (e["type"] == "task") for e in ev), "task_id только у событий типа task")
    check(ev[3]["task_id"] == ev[9]["task_id"] == tasks["Прогулка с собакой"]["id"],
          "события «walk» указывают на восстановленную категорию")
    check(ev[0]["task_id"] == tasks["Домашка до 18:00"]["id"], "событие homework → новая категория")
    check([e["payout_id"] for e in ev] == [1, 1, 1, 2, 2, None, None, None, None, None], "payout_id сохранены")
    check(all(e["child_id"] == cid for e in ev) and all(e["author"] is None for e in ev),
          "все записи привязаны к ребёнку, автор пуст")
    pays = q(d, "SELECT * FROM payouts ORDER BY id")
    check([(p["id"], p["amount"], p["child_id"]) for p in pays] == [(1, 350, cid), (2, 1000, cid)],
          "выплаты перенесены с теми же id и суммами")

    with mod.app.test_request_context():
        con = mod.get_db()
        check(mod.balance(con, cid) == 80, "накопление = 80 ₽ (как в v1.7: 30+50−100+70+30)")
        cfg = mod.child_config(con, cid)
        st = mod.compute_stats(con, cfg)
        cats = {n: (c, s) for n, c, s in st["cats"]}
        check(cats.get("Прогулка с собакой (удалена)") == (2, 60), "статистика: «Прогулка с собакой (удалена)» 2 раза, 60 ₽")
        check(cats.get("Домашка до 18:00") == (1, 100), "статистика: активная категория без пометки")
        check((st["earned"], st["fines"], st["paid"], st["balance"]) == (1530, -100, 1350, 80),
              f"статистика: заработано/штрафы/выплачено/накопление = {st['earned']}/{st['fines']}/{st['paid']}/{st['balance']}")
        check(cfg["grade"][9] == 200 and cfg["control"][10] == 1000 and len(cfg["grades"]) == 10, "тарифы оценок на месте")

    c = q(d, "SELECT * FROM contracts")[0]
    check(c["student_full"] == "Иванов Пётр Сергеевич" and c["city"] == "Тверь" and c["end_date"] == "2027-05-31",
          "договор перенесён")
    check([p["name"] for p in q(d, "SELECT name FROM contract_parties ORDER BY sort_order")] ==
          ["Иванов Сергей", "Иванова Анна"], "подписанты перенесены")
    rev = q(d, "SELECT * FROM contract_revisions")
    check(len(rev) == 1 and len(json.loads(rev[0]["snapshot"])["tasks"]) == 4 and rev[0]["effective_from"] == "2026-09-08",
          "создана первая редакция договора со снимком тарифов")
    check(q(d, "SELECT COUNT(*) c FROM grade_tariffs")[0]["c"] == 20, "20 строк grade_tariffs")
    adm = q(d, "SELECT * FROM users")
    check(len(adm) == 1 and adm[0]["role"] == "admin" and check_password_hash(adm[0]["password_hash"], "pw1"),
          "администратор создан из ADMIN_PASSWORD (хэш)")
    check(sha(os.path.join(d, "tariffs.json")) == tj, "tariffs.json не изменён")
    idx = {r["name"] for r in q(d, "SELECT name FROM sqlite_master WHERE type='index'")}
    check({"ix_events_child_payout", "ix_events_child_date", "ix_payouts_child",
           "ux_sessions_token", "ux_users_login", "ux_children_link"} <= idx, "индексы созданы")

    # повторный запуск: ничего не меняется
    load_app(d, ADMIN_PASSWORD="other")
    check(len(glob.glob(os.path.join(d, "uchet.db.bak-v1.7-*"))) == 1, "повторный запуск: новых копий нет")
    check(q(d, "SELECT COUNT(*) c FROM events")[0]["c"] == 10 and q(d, "SELECT COUNT(*) c FROM children")[0]["c"] == 1,
          "повторный запуск: данные те же")
    h = q(d, "SELECT password_hash h FROM users")[0]["h"]
    check(check_password_hash(h, "pw1") and not check_password_hash(h, "other"),
          "ADMIN_PASSWORD НЕ перезаписал пароль без флага сброса")
    load_app(d, ADMIN_PASSWORD="other", RESET_ADMIN_PASSWORD="1")
    h = q(d, "SELECT password_hash h FROM users")[0]["h"]
    check(check_password_hash(h, "other"), "RESET_ADMIN_PASSWORD=1 сбросил пароль")
    return d


def scenario_b():
    print("\n[B] v1.7 с самым старым форматом tariffs.json (без tasks) и событием удалённой категории t1a2b3")
    d = tempfile.mkdtemp()
    build_v17(d, {"student": "Максим", "homework": 120, "dishes": 40, "trash": 60, "extra_payout_limit": 1000,
                  "grade": GRADE, "control": CONTROL})
    mod = load_app(d, ADMIN_PASSWORD="pw")
    t = {r["name"]: r for r in q(d, "SELECT * FROM tasks")}
    check(t["Домашка до 18:00"]["amount"] == 120 and t["Посудомойка"]["amount"] == 40 and t["Мусор"]["amount"] == 60,
          "суммы из старого формата подхвачены")
    check(t["Уборка комнаты"]["is_deleted"] == 1 and t["Прогулка с собакой"]["is_deleted"] == 1,
          "обе отсутствующие категории восстановлены как удалённые")
    with mod.app.test_request_context():
        check(mod.balance(mod.get_db(), q(d, "SELECT id FROM children")[0]["id"]) == 80, "накопление = 80 ₽")
    check(q(d, "SELECT city FROM contracts")[0]["city"] == "", "договор взят из значений по умолчанию")


def scenario_c():
    print("\n[C] свежая установка (нет ни БД, ни tariffs.json)")
    d = tempfile.mkdtemp()
    load_app(d, ADMIN_PASSWORD="pw")
    check(q(d, "SELECT name FROM children")[0]["name"] == "Максим", "ребёнок «Максим» по умолчанию")
    check(q(d, "SELECT COUNT(*) c FROM tasks WHERE is_deleted=0")[0]["c"] == 3, "3 категории по умолчанию")
    check(q(d, "SELECT COUNT(*) c FROM events")[0]["c"] == 0, "записей нет")
    check(not os.path.exists(os.path.join(d, "tariffs.json")), "tariffs.json не создаётся")
    check(glob.glob(os.path.join(d, "uchet.db.bak*")) == [], "резервная копия не нужна")
    check(len(q(d, "SELECT * FROM users")) == 1, "администратор создан")


def scenario_d():
    print("\n[D] без ADMIN_PASSWORD: аккаунтов нет, вход невозможен")
    d = tempfile.mkdtemp()
    mod = load_app(d)
    check(q(d, "SELECT COUNT(*) c FROM users")[0]["c"] == 0, "администратор не создан")
    cl = mod.app.test_client()
    r = cl.get("/login")
    check(r.status_code == 200 and "Аккаунтов ещё нет" in r.get_data(as_text=True), "страница входа объясняет, что делать")
    check(cl.get("/").status_code == 302, "главная закрыта")


def scenario_e():
    print("\n[E] ошибка миграции: битый tariffs.json → откат, БД не тронута")
    d = tempfile.mkdtemp()
    build_v17(d, None)
    Path(d, "tariffs.json").write_text("{ это не json", encoding="utf-8")
    try:
        load_app(d, ADMIN_PASSWORD="pw")
        check(False, "ожидали остановку запуска")
    except SystemExit as e:
        check(e.code == 1, "запуск остановлен с кодом 1")
    cols = [r["name"] for r in q(d, "PRAGMA table_info(events)")]
    check("kind" in cols and "child_id" not in cols, "events осталась в формате v1.7")
    check(q(d, "SELECT COUNT(*) c FROM events")[0]["c"] == 10, "все 10 записей на месте")
    check(q(d, "SELECT name FROM sqlite_master WHERE name='meta'") == [], "meta не создана — миграция откатилась")

    print("\n[E2] сбой ВНУТРИ транзакции (после переименования таблиц) → откат DDL и данных")
    d2 = tempfile.mkdtemp()
    build_v17(d2, {"student": "Х", "tasks": TASKS_NEW, "extra_payout_limit": 1000, "contract": CONTRACT,
                   "grade": GRADE, "control": CONTROL})
    con = sqlite3.connect(os.path.join(d2, "uchet.db"), isolation_level=None)
    con.execute("CREATE TABLE tasks(foo TEXT)")  # посторонняя таблица: INSERT INTO tasks(child_id…) упадёт уже в транзакции
    con.close()
    try:
        load_app(d2, ADMIN_PASSWORD="pw")
        check(False, "ожидали остановку запуска")
    except SystemExit as e:
        check(e.code == 1, "запуск остановлен с кодом 1")
    names = {r["name"] for r in q(d2, "SELECT name FROM sqlite_master WHERE type='table'")}
    check({"events", "payouts"} <= names and not names & {"events_v17", "payouts_v17", "meta", "children"},
          "переименование таблиц откатилось, новых таблиц нет")
    check(q(d2, "SELECT COUNT(*) c FROM events")[0]["c"] == 10 and "kind" in
          [r["name"] for r in q(d2, "PRAGMA table_info(events)")], "events осталась в формате v1.7, 10 записей")
    check(len(glob.glob(os.path.join(d2, "uchet.db.bak-v1.7-*"))) == 1, "резервная копия создана до миграции")


UA_IPHONE = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
             "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1")


def csrf(cl):
    with cl.session_transaction() as s:
        return s.get("csrf", "")


def post(cl, url, follow=False, **data):
    data["csrf"] = csrf(cl)
    return cl.post(url, data=data, follow_redirects=follow)


def text(r):
    return r.get_data(as_text=True)


def do_login(cl, login, pw, remember=False, ua="", ip="127.0.0.1"):
    cl.get("/login")  # получить CSRF-токен
    data = {"csrf": csrf(cl), "login": login, "password": pw}
    if remember:
        data["remember"] = "1"
    return cl.post("/login", data=data, headers={"User-Agent": ua} if ua else {},
                   environ_overrides={"REMOTE_ADDR": ip})


def set_cookie_header(r):
    return next((h for h in r.headers.getlist("Set-Cookie") if h.startswith("sid=")), "")


def redirects_to_login(r):
    return r.status_code == 302 and "/login" in r.headers.get("Location", "")


def scenario_f(migrated_dir):
    print("\n[F] веб-слой на мигрированной базе (вход по логину, записи, выплата, тарифы)")
    d = tempfile.mkdtemp()
    shutil.rmtree(d)
    shutil.copytree(migrated_dir, d)
    mod = load_app(d, ADMIN_PASSWORD="other")  # пароль после сброса в сценарии A
    cl = mod.app.test_client()

    def balance():
        return q(d, "SELECT COALESCE(SUM(amount),0) b FROM events WHERE payout_id IS NULL")[0]["b"]

    check(redirects_to_login(cl.get("/")), "аноним: главная ведёт на вход")
    check(balance() == 80, "накопление после миграции 80 ₽")
    r = do_login(cl, "admin", "other")
    check(r.status_code == 302 and "/login" not in r.headers["Location"], "вход admin / пароль из БД")
    for url in ("/", "/history", "/stats", "/contract", "/tariffs", "/users", "/account"):
        check(cl.get(url).status_code == 200, f"GET {url} → 200 (администратор)")
    check("Быстрые записи" in text(cl.get("/")), "после входа видны формы записи")

    today = date.today().isoformat()
    hw = q(d, "SELECT id FROM tasks WHERE name='Домашка до 18:00'")[0]["id"]
    post(cl, "/add/quick", kind=str(hw), date=today)
    check(balance() == 180, "быстрая запись «Домашка» +100 → 180")
    post(cl, "/add/quick", kind=str(hw), date=today)
    check(balance() == 180, "повтор «раз в день» за ту же дату заблокирован")
    last = q(d, "SELECT * FROM events ORDER BY id DESC LIMIT 1")[0]
    check(last["type"] == "task" and last["task_id"] == hw and last["author"] is not None,
          "запись: type=task, task_id и автор сохранены")
    post(cl, "/add/grade", kind="control", grade="10", date=today)
    check(balance() == 1180, "контрольная «10» +1000 → 1180")
    post(cl, "/add/grade", kind="student", grade="1", date=today)
    check(balance() == 1180, "подделанный kind отклонён")
    post(cl, "/add/custom", amount="-30", note="штраф", date=today)
    check(balance() == 1150, "своя запись −30 → 1150")

    post(cl, "/payout", amount="150", expected="1000")
    check(balance() == 1150, "выплата с устаревшей суммой отклонена")
    post(cl, "/payout", amount="150", expected="1150")
    p = q(d, "SELECT * FROM payouts ORDER BY id DESC LIMIT 1")[0]
    check(p["amount"] == 150 and p["user_id"] is not None and balance() == 1000,
          "частичная выплата 150: сохранён user_id, остаток 1000 перенесён")
    check(q(d, "SELECT type FROM events ORDER BY id DESC LIMIT 1")[0]["type"] == "carry", "создан carry")
    post(cl, "/undo")
    check(balance() == 1000, "«Отменить последнюю» не трогает carry")

    post(cl, "/add/custom", amount="5", note="тест", date=today)
    new_id = q(d, "SELECT id FROM events ORDER BY id DESC LIMIT 1")[0]["id"]
    post(cl, f"/delete/{new_id}")
    check(q(d, "SELECT COUNT(*) c FROM events WHERE id=?", (new_id,))[0]["c"] == 0, "удаление невыплаченной записи")
    post(cl, "/delete/1")
    check(q(d, "SELECT COUNT(*) c FROM events WHERE id=1")[0]["c"] == 1, "выплаченную запись удалить нельзя")

    # сохранение тарифов: переименовать «Мусор», удалить «Посудомойка», добавить новую категорию
    with mod.app.test_request_context():
        cfg = mod.child_config(mod.get_db(), q(d, "SELECT id FROM children")[0]["id"])
    form = {"student": "Пётр", "limit": "800", "task_idx": [], "number": "1/2026", "city": "Тверь",
            "cdate": "2026-09-08", "cend": "2027-05-31", "student_full": "Иванов Пётр Сергеевич",
            "student_age": "12", "student_class": "6", "p1_name": "Иванов Сергей", "p1_role": "Папа", "p1_sex": "m",
            "p2_name": "", "p2_role": "", "p2_sex": "f"}
    for g_ in cfg["grades"]:
        form[f"g{g_}"], form[f"c{g_}"] = str(cfg["grade"][g_]), str(cfg["control"][g_])
    for x in cfg["tasks"]:
        i = str(x["id"])
        form["task_idx"].append(i)
        form[f"task_{i}_id"] = i
        form[f"task_{i}_name"] = "Вынос мусора" if x["name"] == "Мусор" else x["name"]
        form[f"task_{i}_amount"] = str(x["amount"])
        if x["once_per_day"]:
            form[f"task_{i}_once"] = "on"
        if x["name"] == "Посудомойка":
            form[f"task_{i}_del"] = "on"
    form["task_idx"].append("n0")
    form.update({"task_n0_id": "", "task_n0_name": "Полив цветов", "task_n0_amount": "40", "task_n0_once": "on"})
    post(cl, "/tariffs", **form)
    names = {r["name"]: r for r in q(d, "SELECT * FROM tasks")}
    check("Вынос мусора" in names and "Мусор" not in names, "категория переименована")
    check(q(d, "SELECT COUNT(*) c FROM events WHERE descr='Вынос мусора'")[0]["c"] == 1,
          "переименование обновило и старую запись")
    check(names["Посудомойка"]["is_deleted"] == 1
          and q(d, "SELECT COUNT(*) c FROM events WHERE task_id=?", (names["Посудомойка"]["id"],))[0]["c"] == 1,
          "удаление — мягкое, старая запись осталась")
    check(names["Полив цветов"]["amount"] == 40 and names["Полив цветов"]["once_per_day"] == 1, "новая категория добавлена")
    check(q(d, "SELECT extra_payout_limit l FROM children")[0]["l"] == 800, "лимит сохранён")
    check("Посудомойка (удалена)" in text(cl.get("/stats")), "в статистике: «Посудомойка (удалена)»")
    check("Иванов Пётр Сергеевич" in text(cl.get("/contract")), "договор показывает данные из БД")

    post(cl, "/tariffs", **dict(form, limit="abc"))
    check(q(d, "SELECT extra_payout_limit l FROM children")[0]["l"] == 800, "ошибка ввода — ничего не сохраняется")
    post(cl, "/logout")
    check(redirects_to_login(cl.get("/")), "после выхода главная снова закрыта")


def scenario_g():
    print("\n[G] аккаунты, роли, сессии, «запомнить меня», защита от перебора")
    d = tempfile.mkdtemp()
    mod = load_app(d, ADMIN_PASSWORD="adminpass1")
    A = mod.app.test_client()
    today = date.today().isoformat()

    def balance():
        return q(d, "SELECT COALESCE(SUM(amount),0) b FROM events WHERE payout_id IS NULL")[0]["b"]

    def user_id(login):
        return q(d, "SELECT id FROM users WHERE login=?", (login,))[0]["id"]

    def temp_password(r):
        m = re.search(r"Временный пароль: (\S+) —", text(r))
        return m.group(1) if m else None

    def first_login(cl, login, temp, new, **kw):
        do_login(cl, login, temp, **kw)
        r = post(cl, "/account/password", current=temp, new=new, repeat=new)
        return r

    # — 1. аноним ————————————————————————————————————————————————
    for url in ("/", "/history", "/stats", "/contract", "/tariffs", "/users", "/account"):
        check(redirects_to_login(A.get(url)), f"аноним: {url} → вход")
    for url in ("/login", "/healthz", "/manifest.webmanifest", "/static/style.css"):
        check(A.get(url).status_code == 200, f"аноним: {url} доступен")
    r = A.get("/login")
    check("Вход" in text(r) and "Максим" not in text(r), "страница входа не раскрывает имя ребёнка")
    check(redirects_to_login(post(A, "/add/quick", kind="1", date=today)), "аноним не может писать")

    # — 2. вход ————————————————————————————————————————————————
    t1 = text(do_login(A, "admin", "nope"))
    t2 = text(do_login(A, "ghost", "nope"))
    check("Неверный логин или пароль" in t1 and "Неверный логин или пароль" in t2,
          "неверный пароль и несуществующий логин — одинаковое сообщение")
    r = do_login(A, "ADMIN", "adminpass1", ua=UA_IPHONE)
    check(r.status_code == 302, "логин нечувствителен к регистру")
    sc = set_cookie_header(r)
    check("HttpOnly" in sc and "SameSite=Lax" in sc and "Max-Age" not in sc and "Expires" not in sc and "Secure" not in sc,
          "cookie: HttpOnly, SameSite=Lax, без срока (сеансовая), без Secure по умолчанию")
    tok = A.get_cookie("sid").value
    row = q(d, "SELECT * FROM sessions")[0]
    check(row["token_hash"] == hashlib.sha256(tok.encode()).hexdigest() and row["token_hash"] != tok,
          "в БД хранится хэш токена, а не сам токен")
    check(row["remember"] == 0 and abs(row["expires"] - (time.time() + 12 * 3600)) < 60, "без «запомнить»: срок SESSION_HOURS=12 ч")
    check("iPhone · Safari" in row["device"], "описание устройства: " + row["device"])

    # — 3. «запомнить меня», скользящий срок, истечение ———————————————
    B = mod.app.test_client()
    r = do_login(B, "admin", "adminpass1", remember=True)
    check("Max-Age=2592000" in set_cookie_header(r), "«запомнить»: cookie на 30 дней")
    rb = q(d, "SELECT * FROM sessions WHERE remember=1")[0]
    check(abs(rb["expires"] - (time.time() + 30 * 86400)) < 60, "«запомнить»: срок в БД 30 дней")
    now = int(time.time())
    q_exec = lambda sql, args=(): (lambda c: (c.execute(sql, args), c.commit(), c.close()))(
        sqlite3.connect(os.path.join(d, "uchet.db")))
    q_exec("UPDATE sessions SET last_seen=?, expires=? WHERE id=?", (now - 7200, now + 100, rb["id"]))
    r = B.get("/")
    check(r.status_code == 200 and abs(q(d, "SELECT expires e FROM sessions WHERE id=?", (rb["id"],))[0]["e"]
                                       - (time.time() + 30 * 86400)) < 60, "скользящее продление: срок сдвинут на 30 дней")
    check("Max-Age=2592000" in set_cookie_header(r), "скользящее продление: cookie обновлена")
    q_exec("UPDATE sessions SET last_seen=?, expires=? WHERE id=?", (now - 10, now + 500, rb["id"]))
    B.get("/")
    check(abs(q(d, "SELECT expires e FROM sessions WHERE id=?", (rb["id"],))[0]["e"] - (now + 500)) <= 1,
          "в пределах часа срок не переписывается (нет записи в БД на каждый запрос)")
    ra = q(d, "SELECT id FROM sessions WHERE remember=0")[0]["id"]
    q_exec("UPDATE sessions SET last_seen=?, expires=? WHERE id=?", (now - 7200, now + 100, ra))
    A.get("/")
    check(abs(q(d, "SELECT expires e FROM sessions WHERE id=?", (ra,))[0]["e"] - (time.time() + 12 * 3600)) < 60,
          "обычная сессия тоже скользящая (12 ч)")
    q_exec("UPDATE sessions SET expires=? WHERE id=?", (now - 5, rb["id"]))
    check(redirects_to_login(B.get("/")), "просроченная сессия → вход")
    check(q(d, "SELECT COUNT(*) c FROM sessions WHERE id=?", (rb["id"],))[0]["c"] == 0, "просроченная сессия удалена из БД")

    for val, want in (("5", 30), ("200", 90), ("45", 45), ("abc", 30)):
        m = load_app(tempfile.mkdtemp(), ADMIN_PASSWORD="x", REMEMBER_DAYS=val)
        check(m.REMEMBER_DAYS == want, f"REMEMBER_DAYS={val} → {want}")
    m = load_app(tempfile.mkdtemp(), ADMIN_PASSWORD="adminpass1", COOKIE_SECURE="1")
    r = do_login(m.app.test_client(), "admin", "adminpass1")
    check("Secure" in set_cookie_header(r), "COOKIE_SECURE=1 → cookie с Secure")
    with mod.app.test_request_context():
        check(mod.get_db().execute("PRAGMA foreign_keys").fetchone()[0] == 1, "PRAGMA foreign_keys=ON")

    # — 4. создание аккаунтов ————————————————————————————————————
    cid = q(d, "SELECT id FROM children")[0]["id"]
    n0 = q(d, "SELECT COUNT(*) c FROM users")[0]["c"]
    A.post("/users/create", data={"login": "mom", "display_name": "Мама", "role": "adult"})
    check(q(d, "SELECT COUNT(*) c FROM users")[0]["c"] == n0, "создание без CSRF-токена отклонено")
    post(A, "/users/create", login="ab", display_name="X", role="adult")
    post(A, "/users/create", login="bad login", display_name="X", role="adult")
    post(A, "/users/create", login="okname", display_name="X", role="root")
    post(A, "/users/create", login="okname", display_name="X", role="adult", password="123")
    check(q(d, "SELECT COUNT(*) c FROM users")[0]["c"] == n0, "короткий логин / пробел / неверная роль / короткий пароль отклонены")
    r = post(A, "/users/create", follow=True, login="mom", display_name="<b>Мама</b>", role="adult", all_children="on")
    mom_temp = temp_password(r)
    mom = q(d, "SELECT * FROM users WHERE login='mom'")[0]
    check(mom_temp and len(mom_temp) == 10, "временный пароль создан автоматически и показан один раз")
    check(mom["must_change_password"] == 1 and mom["can_payout"] == 0 and mom["all_children"] == 1 and mom["role"] == "adult",
          "аккаунт: смена пароля при входе, выплаты выключены по умолчанию")
    check(check_password_hash(mom["password_hash"], mom_temp), "пароль хранится хэшем")
    check("&lt;b&gt;Мама&lt;/b&gt;" in text(A.get("/users")) and "<b>Мама</b>" not in text(A.get("/users")),
          "имя с HTML экранируется (XSS)")
    r = post(A, "/users/create", follow=True, login="MOM", display_name="Дубль", role="adult")
    check("Такой логин уже есть" in text(r), "логин уникален без учёта регистра")

    # — 5. первый вход взрослого: обязательная смена пароля ——————————
    M, M2 = mod.app.test_client(), mod.app.test_client()
    do_login(M2, "mom", mom_temp)
    do_login(M, "mom", mom_temp)
    r = M.get("/")
    check(r.status_code == 302 and "/account" in r.headers["Location"], "до смены пароля всё ведёт на «Мой аккаунт»")
    check("смените временный пароль" in text(M.get("/account")), "страница смены пароля показана")
    n_ev = q(d, "SELECT COUNT(*) c FROM events")[0]["c"]
    hw = q(d, "SELECT id FROM tasks WHERE name='Домашка до 18:00'")[0]["id"]
    dishes = q(d, "SELECT id FROM tasks WHERE name='Посудомойка'")[0]["id"]
    post(M, "/add/quick", kind=str(hw), date=today)
    check(q(d, "SELECT COUNT(*) c FROM events")[0]["c"] == n_ev, "пока пароль не сменён, записи не принимаются")
    check("неверный" in text(post(M, "/account/password", follow=True, current="x", new="newpass123", repeat="newpass123")),
          "неверный текущий пароль")
    check("минимум 8" in text(post(M, "/account/password", follow=True, current=mom_temp, new="short", repeat="short")),
          "короткий новый пароль")
    check("не совпадают" in text(post(M, "/account/password", follow=True, current=mom_temp, new="newpass123", repeat="other123")),
          "пароли не совпадают")
    r = post(M, "/account/password", current=mom_temp, new="momnewpass1", repeat="momnewpass1")
    check(r.status_code == 302 and q(d, "SELECT must_change_password m FROM users WHERE login='mom'")[0]["m"] == 0,
          "пароль сменён, флаг снят")
    check(redirects_to_login(M2.get("/")), "после смены пароля другие устройства отключены")
    check(M.get("/").status_code == 200, "текущее устройство осталось в системе")

    # — 6. права взрослого ———————————————————————————————————————
    mom_id = user_id("mom")
    post(A, "/add/custom", amount="500", note="от админа", date=today)
    admin_ev = q(d, "SELECT id FROM events ORDER BY id DESC LIMIT 1")[0]["id"]
    check(q(d, "SELECT author FROM events WHERE id=?", (admin_ev,))[0]["author"] == user_id("admin"), "автор записи — администратор")
    html = text(M.get("/"))
    check("Быстрые записи" in html and 'href="/tariffs"' not in html and "Выплата произведена" not in html,
          "взрослый: формы записи есть; «Тарифы» и кнопки выплаты нет")
    r = M.get("/tariffs", follow_redirects=True)
    check("Недостаточно прав" in text(r), "взрослый: /tariffs закрыт")
    check("Недостаточно прав" in text(M.get("/users", follow_redirects=True)), "взрослый: /users закрыт")
    post(M, "/tariffs", student="Взлом", limit="1")
    check(q(d, "SELECT name FROM children")[0]["name"] == "Максим", "взрослый: POST /tariffs ничего не меняет")
    check(M.get("/contract").status_code == 200 and M.get("/history").status_code == 200, "взрослый видит договор и историю")
    post(M, "/add/quick", kind=str(dishes), date=today)
    mom_ev = q(d, "SELECT * FROM events ORDER BY id DESC LIMIT 1")[0]
    check(mom_ev["author"] == mom_id and balance() == 550, "взрослый добавил запись, автор сохранён")
    post(M, "/payout", amount="550", expected="550")
    check(balance() == 550 and q(d, "SELECT COUNT(*) c FROM payouts")[0]["c"] == 0, "без can_payout выплата отклонена на сервере")
    post(M, f"/delete/{admin_ev}")
    check(q(d, "SELECT COUNT(*) c FROM events WHERE id=?", (admin_ev,))[0]["c"] == 1, "чужую запись удалить нельзя")
    con = sqlite3.connect(os.path.join(d, "uchet.db"))
    con.execute("INSERT INTO events(child_id,date,type,descr,amount) VALUES(?,?,?,?,?)", (cid, today, "custom", "из v1.7", 7))
    con.commit()
    con.close()
    legacy_ev = q(d, "SELECT id FROM events WHERE descr='из v1.7'")[0]["id"]
    post(M, f"/delete/{legacy_ev}")
    check(q(d, "SELECT COUNT(*) c FROM events WHERE id=?", (legacy_ev,))[0]["c"] == 1, "запись без автора (из v1.7) взрослый удалить не может")
    post(A, "/add/custom", amount="3", note="после мамы", date=today)  # последняя запись — админа
    before = balance()
    post(M, "/undo")
    check(q(d, "SELECT COUNT(*) c FROM events WHERE id=?", (mom_ev["id"],))[0]["c"] == 0
          and q(d, "SELECT COUNT(*) c FROM events WHERE descr='после мамы'")[0]["c"] == 1 and balance() == before - 50,
          "«Отменить» у взрослого снимает его запись, а не последнюю чужую")
    post(M, "/add/quick", kind=str(dishes), date=today)
    own = q(d, "SELECT id FROM events ORDER BY id DESC LIMIT 1")[0]["id"]
    post(M, f"/delete/{own}")
    check(q(d, "SELECT COUNT(*) c FROM events WHERE id=?", (own,))[0]["c"] == 0, "свою запись взрослый удаляет")

    # — 7. право на выплаты —————————————————————————————————————
    post(A, f"/users/{mom_id}", display_name="Мама", role="adult", can_payout="on", all_children="on", is_active="on")
    check("Выплата произведена" in text(M.get("/")), "can_payout включён: кнопка выплаты появилась")
    bal = balance()
    post(M, "/payout", amount="100", expected=str(bal))
    pay = q(d, "SELECT * FROM payouts ORDER BY id DESC LIMIT 1")[0]
    check(pay["amount"] == 100 and pay["user_id"] == mom_id and balance() == bal - 100,
          "взрослый с can_payout отметил выплату, user_id = его id; carry создан")

    # — 8. доступ к детям ———————————————————————————————————————
    r = post(A, "/users/create", follow=True, login="dad", display_name="Папа", role="adult")  # все дети выкл.
    dad_temp = temp_password(r)
    D = mod.app.test_client()
    first_login(D, "dad", dad_temp, "dadnewpass1")
    r = D.get("/")
    check(r.status_code == 200 and "не назначен ни один ребёнок" in text(r), "без назначенных детей — страница «нет доступа»")
    n_ev = q(d, "SELECT COUNT(*) c FROM events")[0]["c"]
    post(D, "/add/quick", kind=str(hw), date=today)
    check(q(d, "SELECT COUNT(*) c FROM events")[0]["c"] == n_ev, "без доступа к ребёнку запись невозможна")
    check(D.get("/history").status_code == 200 and "не назначен" in text(D.get("/history")) and D.get("/account").status_code == 200,
          "история закрыта, аккаунт доступен")
    dad_id = user_id("dad")
    post(A, f"/users/{dad_id}", display_name="Папа", role="adult", child=str(cid), is_active="on")
    check("Быстрые записи" in text(D.get("/")), "после назначения ребёнка доступ появился")

    # — 9. отключение, роли, самозащита —————————————————————————
    post(A, f"/users/{dad_id}", display_name="Папа", role="adult", child=str(cid))  # is_active не передан
    check(redirects_to_login(D.get("/")) and q(d, "SELECT COUNT(*) c FROM sessions WHERE subject_id=?", (dad_id,))[0]["c"] == 0,
          "отключённый аккаунт: сессии удалены, вход закрыт")
    check("Неверный логин или пароль" in text(do_login(mod.app.test_client(), "dad", "dadnewpass1")),
          "отключённый аккаунт не входит (сообщение не раскрывает причину)")
    post(A, f"/users/{dad_id}", display_name="Папа", role="adult", child=str(cid), is_active="on")
    check(do_login(mod.app.test_client(), "dad", "dadnewpass1").status_code == 302, "после включения вход работает")
    admin_id = user_id("admin")
    post(A, f"/users/{admin_id}", display_name="Админ", role="adult")
    me = q(d, "SELECT role,is_active FROM users WHERE id=?", (admin_id,))[0]
    check(me["role"] == "admin" and me["is_active"] == 1, "администратор не может понизить/отключить сам себя")
    post(A, f"/users/{dad_id}", display_name="Папа", role="admin", is_active="on")
    dd = q(d, "SELECT role,all_children,can_payout FROM users WHERE id=?", (dad_id,))[0]
    check(dd["role"] == "admin" and dd["all_children"] == 1 and dd["can_payout"] == 1, "роль «Администратор» даёт все права")
    post(A, f"/users/{dad_id}", display_name="Папа", role="adult", child=str(cid), is_active="on")

    # — 10. устройства ——————————————————————————————————————————
    M3 = mod.app.test_client()
    do_login(M3, "mom", "momnewpass1")
    ids = [r["id"] for r in q(d, "SELECT id FROM sessions WHERE subject_id=? ORDER BY id", (mom_id,))]
    check(len(ids) == 2 and "это устройство" in text(M.get("/account")), "в «Мои устройства» два сеанса, текущий помечен")
    post(M, f"/account/sessions/{ids[1]}/revoke")
    check(redirects_to_login(M3.get("/")), "отзыв устройства: второй сеанс отключён")
    check(M.get("/").status_code == 200, "отзыв чужого устройства не трогает текущее")
    post(M, f"/account/sessions/{admin_id}/revoke")  # чужой id сессии (любой) не должен удалиться
    check(q(d, "SELECT COUNT(*) c FROM sessions WHERE subject_id=?", (admin_id,))[0]["c"] >= 1, "нельзя отозвать сессию другого пользователя")
    M3 = mod.app.test_client()
    do_login(M3, "mom", "momnewpass1")
    sid3 = q(d, "SELECT id FROM sessions WHERE subject_id=? ORDER BY id DESC", (mom_id,))[0]["id"]
    post(A, f"/users/{mom_id}/sessions/{sid3}/revoke")
    check(redirects_to_login(M3.get("/")), "администратор отзывает устройство пользователя")
    M3 = mod.app.test_client()
    do_login(M3, "mom", "momnewpass1")
    post(A, f"/users/{mom_id}/logout-all")
    check(redirects_to_login(M.get("/")) and redirects_to_login(M3.get("/")), "«выйти везде» от администратора")
    do_login(M, "mom", "momnewpass1")
    r = post(M, "/account/logout-all")
    check(redirects_to_login(M.get("/")) and q(d, "SELECT COUNT(*) c FROM sessions WHERE subject_id=?", (mom_id,))[0]["c"] == 0,
          "«выйти на всех устройствах» (свой аккаунт)")

    do_login(M, "mom", "momnewpass1")
    own_sid = q(d, "SELECT id FROM sessions WHERE subject_id=? ORDER BY id DESC", (mom_id,))[0]["id"]
    post(M, f"/account/sessions/{own_sid}/revoke")
    check(redirects_to_login(M.get("/")), "отзыв собственного (текущего) устройства = выход")

    # — 11. сброс пароля администратором —————————————————————————
    do_login(M, "mom", "momnewpass1")
    r = post(A, f"/users/{mom_id}/password", follow=True, password="")
    reset = temp_password(r)
    check(reset and redirects_to_login(M.get("/")), "сброс пароля: новый временный пароль, сеансы отключены")
    check(do_login(mod.app.test_client(), "mom", "momnewpass1").status_code == 200, "старый пароль больше не подходит")
    check(do_login(mod.app.test_client(), "mom", reset).status_code == 302, "временный пароль подходит")
    check(q(d, "SELECT must_change_password m FROM users WHERE id=?", (mom_id,))[0]["m"] == 1, "снова требуется смена пароля")
    post(A, f"/users/{admin_id}/password", password="whatever123")
    check(check_password_hash(q(d, "SELECT password_hash h FROM users WHERE id=?", (admin_id,))[0]["h"], "adminpass1"),
          "пароль самого администратора через /users не сбрасывается")

    # — 12. защита от перебора (в БД, на логин и на IP) ———————————
    C = lambda: mod.app.test_client()  # новый клиент на каждую попытку (иначе сработает «уже вошли»)
    for i in range(1, 6):
        do_login(C(), "dad", "bad", ip=f"10.0.0.{i}")
    check("Слишком много попыток" in text(do_login(C(), "dad", "dadnewpass1", ip="10.0.0.99")),
          "5 ошибок на один логин (с разных IP) → блокировка логина")
    check(q(d, "SELECT COUNT(*) c FROM login_attempts WHERE scope='login' AND key='dad' AND locked_until>?", (time.time(),))[0]["c"] == 1,
          "блокировка хранится в БД")
    check(q(d, "SELECT COUNT(*) c FROM audit_log WHERE action='login_locked'")[0]["c"] >= 1, "блокировка записана в журнал")
    q_exec("UPDATE login_attempts SET locked_until=0")
    check(do_login(C(), "dad", "dadnewpass1", ip="10.0.0.99").status_code == 302, "после окончания блокировки вход работает")
    for i in range(5):
        do_login(C(), f"nobody{i}", "bad", ip="10.9.9.9")
    check("Слишком много попыток" in text(do_login(C(), "admin", "adminpass1", ip="10.9.9.9")),
          "5 ошибок с одного IP (разные логины) → блокировка IP")
    check(do_login(C(), "admin", "adminpass1", ip="10.8.8.8").status_code == 302, "другой IP не затронут")
    m2 = load_app(d, ADMIN_PASSWORD="adminpass1")
    check("Слишком много попыток" in text(do_login(m2.app.test_client(), "admin", "adminpass1", ip="10.9.9.9")),
          "блокировка переживает перезапуск приложения (не в памяти процесса)")
    do_login(C(), "bob2", "x", ip="10.7.7.7")
    q_exec("UPDATE login_attempts SET fails=4, updated=? WHERE scope='login' AND key='bob2'", (time.time() - 1000,))
    do_login(C(), "bob2", "x", ip="10.7.7.8")
    check(q(d, "SELECT fails f FROM login_attempts WHERE scope='login' AND key='bob2'")[0]["f"] == 1,
          "старые ошибки «остывают» и не приводят к блокировке")
    do_login(C(), "mom", "bad", ip="10.6.6.6")
    do_login(C(), "mom", reset, ip="10.6.6.7")
    check(q(d, "SELECT COUNT(*) c FROM login_attempts WHERE scope='login' AND key='mom'")[0]["c"] == 0,
          "успешный вход обнуляет счётчик логина")

    # — 13. журнал ———————————————————————————————————————————
    acts = {r["action"] for r in q(d, "SELECT DISTINCT action FROM audit_log")}
    want = {"login", "login_fail", "login_locked", "logout_all", "user_create", "user_update", "user_disable",
            "user_enable", "password_change", "password_reset", "session_revoke"}
    check(want <= acts, "в журнале есть: " + ", ".join(sorted(want)) if want <= acts else f"не хватает: {sorted(want - acts)}")
    check(all(a is not None for a in [q(d, "SELECT ts FROM audit_log LIMIT 1")[0]["ts"]]), "записи журнала с временем")
    html = text(A.get("/users"))
    check("Последние входы" in html and "mom" in html and "блокировка" in html, "администратор видит список входов")
    post(A, "/logout")
    check(q(d, "SELECT COUNT(*) c FROM audit_log WHERE action='logout'")[0]["c"] >= 1, "выход записан в журнал")

    # — 14. аварийный сброс —————————————————————————————————————
    do_login(A, "admin", "adminpass1", ip="10.5.5.5")
    check(A.get("/").status_code == 200, "администратор вошёл перед сбросом")
    load_app(d, ADMIN_PASSWORD="resetpass1", RESET_ADMIN_PASSWORD="1")
    check(redirects_to_login(A.get("/")), "RESET_ADMIN_PASSWORD=1: сессии администратора отключены")
    check(q(d, "SELECT COUNT(*) c FROM login_attempts")[0]["c"] == 0, "RESET_ADMIN_PASSWORD=1: блокировки сняты")
    check(do_login(mod.app.test_client(), "admin", "resetpass1").status_code == 302, "вход с новым паролем")


if __name__ == "__main__":
    mig = scenario_a()
    scenario_b()
    scenario_c()
    scenario_d()
    scenario_e()
    scenario_f(mig)
    scenario_g()
    print()
    if FAILED:
        print(f"ПРОВАЛЕНО проверок: {len(FAILED)}")
        for m in FAILED:
            print("  -", m)
        sys.exit(1)
    print("Все проверки пройдены.")
