# -*- coding: utf-8 -*-
"""
Проверка миграции v1.7 → 2.0 (этап 1) на тестовой БД, созданной по схеме v1.7,
аккаунтов/ролей/сессий (этап 2), нескольких детей (этап 3)
доступа ребёнка по ссылке/PIN (этап 4)
договора: редакции, шкала, правила (этап 5)
и журнала действий, документации и сквозного сценария (этап 6).
Запуск из корня репозитория:  python tests/test_migration.py
В образ не попадает (Dockerfile копирует только app.py, templates/, static/).
"""
import glob
import hashlib
import importlib.util
import itertools
import contextlib
import io
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta
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
    for k in ("ADMIN_PASSWORD", "RESET_ADMIN_PASSWORD", "CONFIG_DIR", "AUDIT_KEEP_DAYS", "REMEMBER_DAYS", "BASE_URL",
              "COOKIE_SECURE", "SESSION_HOURS", "TRUST_PROXY"):
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
    sm = q(d, "SELECT * FROM audit_log WHERE action='system_migrate'")
    check(len(sm) == 1 and sm[0]["actor"] == "система" and "uchet.db.bak-v1.7-" in sm[0]["details"], "миграция записана в журнал с именем резервной копии")
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
    cid = q(d, "SELECT id FROM children")[0]["id"]
    P = lambda path: f"/c/{cid}{path}"
    HOME = P("/")

    def balance():
        return q(d, "SELECT COALESCE(SUM(amount),0) b FROM events WHERE payout_id IS NULL")[0]["b"]

    check(redirects_to_login(cl.get("/")), "аноним: главная ведёт на вход")
    check(balance() == 80, "накопление после миграции 80 ₽")
    r = do_login(cl, "admin", "other")
    check(r.status_code == 302 and "/login" not in r.headers["Location"], "вход admin / пароль из БД")
    for url in (HOME, P("/history"), P("/stats"), P("/contract"), P("/tariffs"), "/users", "/children", "/account"):
        check(cl.get(url).status_code == 200, f"GET {url} → 200 (администратор)")
    r = cl.get("/")
    check(r.status_code == 302 and r.headers["Location"].endswith(HOME), "один ребёнок: «/» ведёт сразу на его обзор")
    check("Быстрые записи" in text(cl.get(HOME)), "после входа видны формы записи")

    today = date.today().isoformat()
    hw = q(d, "SELECT id FROM tasks WHERE name='Домашка до 18:00'")[0]["id"]
    post(cl, P("/add/quick"), kind=str(hw), date=today)
    check(balance() == 180, "быстрая запись «Домашка» +100 → 180")
    post(cl, P("/add/quick"), kind=str(hw), date=today)
    check(balance() == 180, "повтор «раз в день» за ту же дату заблокирован")
    last = q(d, "SELECT * FROM events ORDER BY id DESC LIMIT 1")[0]
    check(last["type"] == "task" and last["task_id"] == hw and last["author"] is not None,
          "запись: type=task, task_id и автор сохранены")
    post(cl, P("/add/grade"), kind="control", grade="10", date=today)
    check(balance() == 1180, "контрольная «10» +1000 → 1180")
    post(cl, P("/add/grade"), kind="student", grade="1", date=today)
    check(balance() == 1180, "подделанный kind отклонён")
    post(cl, P("/add/custom"), amount="-30", note="штраф", date=today)
    check(balance() == 1150, "своя запись −30 → 1150")

    post(cl, P("/payout"), amount="150", expected="1000")
    check(balance() == 1150, "выплата с устаревшей суммой отклонена")
    post(cl, P("/payout"), amount="150", expected="1150")
    p = q(d, "SELECT * FROM payouts ORDER BY id DESC LIMIT 1")[0]
    check(p["amount"] == 150 and p["user_id"] is not None and balance() == 1000,
          "частичная выплата 150: сохранён user_id, остаток 1000 перенесён")
    check(q(d, "SELECT type FROM events ORDER BY id DESC LIMIT 1")[0]["type"] == "carry", "создан carry")
    post(cl, P("/undo"))
    check(balance() == 1000, "«Отменить последнюю» не трогает carry")

    post(cl, P("/add/custom"), amount="5", note="тест", date=today)
    new_id = q(d, "SELECT id FROM events ORDER BY id DESC LIMIT 1")[0]["id"]
    post(cl, P(f"/delete/{new_id}"))
    check(q(d, "SELECT COUNT(*) c FROM events WHERE id=?", (new_id,))[0]["c"] == 0, "удаление невыплаченной записи")
    post(cl, P("/delete/1"))
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
    post(cl, P("/tariffs"), **form)
    names = {r["name"]: r for r in q(d, "SELECT * FROM tasks")}
    check("Вынос мусора" in names and "Мусор" not in names, "категория переименована")
    check(q(d, "SELECT COUNT(*) c FROM events WHERE descr='Вынос мусора'")[0]["c"] == 1,
          "переименование обновило и старую запись")
    check(names["Посудомойка"]["is_deleted"] == 1
          and q(d, "SELECT COUNT(*) c FROM events WHERE task_id=?", (names["Посудомойка"]["id"],))[0]["c"] == 1,
          "удаление — мягкое, старая запись осталась")
    check(names["Полив цветов"]["amount"] == 40 and names["Полив цветов"]["once_per_day"] == 1, "новая категория добавлена")
    check(q(d, "SELECT extra_payout_limit l FROM children")[0]["l"] == 800, "лимит сохранён")
    check("Посудомойка (удалена)" in text(cl.get(P("/stats"))), "в статистике: «Посудомойка (удалена)»")
    check("Иванов Пётр Сергеевич" in text(cl.get(P("/contract"))), "договор показывает данные из БД")

    post(cl, P("/tariffs"), **dict(form, limit="abc"))
    check(q(d, "SELECT extra_payout_limit l FROM children")[0]["l"] == 800, "ошибка ввода — ничего не сохраняется")
    post(cl, "/logout")
    check(redirects_to_login(cl.get("/")), "после выхода главная снова закрыта")


def scenario_g():
    print("\n[G] аккаунты, роли, сессии, «запомнить меня», защита от перебора")
    d = tempfile.mkdtemp()
    mod = load_app(d, ADMIN_PASSWORD="adminpass1")
    A = mod.app.test_client()
    today = date.today().isoformat()
    cid = q(d, "SELECT id FROM children")[0]["id"]
    P = lambda path: f"/c/{cid}{path}"
    HOME = P("/")

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
    for url in ("/", HOME, P("/history"), P("/stats"), P("/contract"), P("/tariffs"), "/history", "/users", "/children", "/account"):
        check(redirects_to_login(A.get(url)), f"аноним: {url} → вход")
    for url in ("/login", "/healthz", "/manifest.webmanifest", "/static/style.css"):
        check(A.get(url).status_code == 200, f"аноним: {url} доступен")
    r = A.get("/login")
    check("Вход" in text(r) and "Максим" not in text(r), "страница входа не раскрывает имя ребёнка")
    check(redirects_to_login(post(A, P("/add/quick"), kind="1", date=today)), "аноним не может писать")

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
    r = B.get(HOME)
    check(r.status_code == 200 and abs(q(d, "SELECT expires e FROM sessions WHERE id=?", (rb["id"],))[0]["e"]
                                       - (time.time() + 30 * 86400)) < 60, "скользящее продление: срок сдвинут на 30 дней")
    check("Max-Age=2592000" in set_cookie_header(r), "скользящее продление: cookie обновлена")
    q_exec("UPDATE sessions SET last_seen=?, expires=? WHERE id=?", (now - 10, now + 500, rb["id"]))
    B.get(HOME)
    check(abs(q(d, "SELECT expires e FROM sessions WHERE id=?", (rb["id"],))[0]["e"] - (now + 500)) <= 1,
          "в пределах часа срок не переписывается (нет записи в БД на каждый запрос)")
    ra = q(d, "SELECT id FROM sessions WHERE remember=0")[0]["id"]
    q_exec("UPDATE sessions SET last_seen=?, expires=? WHERE id=?", (now - 7200, now + 100, ra))
    A.get(HOME)
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
    post(M, P("/add/quick"), kind=str(hw), date=today)
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
    check(M.get(HOME).status_code == 200, "текущее устройство осталось в системе")

    # — 6. права взрослого ———————————————————————————————————————
    mom_id = user_id("mom")
    post(A, P("/add/custom"), amount="500", note="от админа", date=today)
    admin_ev = q(d, "SELECT id FROM events ORDER BY id DESC LIMIT 1")[0]["id"]
    check(q(d, "SELECT author FROM events WHERE id=?", (admin_ev,))[0]["author"] == user_id("admin"), "автор записи — администратор")
    html = text(M.get(HOME))
    check("Быстрые записи" in html and f'href="/c/{cid}/tariffs"' not in html and "Выплата произведена" not in html,
          "взрослый: формы записи есть; «Тарифы» и кнопки выплаты нет")
    r = M.get(P("/tariffs"), follow_redirects=True)
    check("Недостаточно прав" in text(r), "взрослый: /tariffs закрыт")
    check("Недостаточно прав" in text(M.get("/users", follow_redirects=True)), "взрослый: /users закрыт")
    post(M, P("/tariffs"), student="Взлом", limit="1")
    check(q(d, "SELECT name FROM children")[0]["name"] == "Максим", "взрослый: POST /tariffs ничего не меняет")
    check(M.get(P("/contract")).status_code == 200 and M.get(P("/history")).status_code == 200, "взрослый видит договор и историю")
    post(M, P("/add/quick"), kind=str(dishes), date=today)
    mom_ev = q(d, "SELECT * FROM events ORDER BY id DESC LIMIT 1")[0]
    check(mom_ev["author"] == mom_id and balance() == 550, "взрослый добавил запись, автор сохранён")
    post(M, P("/payout"), amount="550", expected="550")
    check(balance() == 550 and q(d, "SELECT COUNT(*) c FROM payouts")[0]["c"] == 0, "без can_payout выплата отклонена на сервере")
    post(M, P(f"/delete/{admin_ev}"))
    check(q(d, "SELECT COUNT(*) c FROM events WHERE id=?", (admin_ev,))[0]["c"] == 1, "чужую запись удалить нельзя")
    con = sqlite3.connect(os.path.join(d, "uchet.db"))
    con.execute("INSERT INTO events(child_id,date,type,descr,amount) VALUES(?,?,?,?,?)", (cid, today, "custom", "из v1.7", 7))
    con.commit()
    con.close()
    legacy_ev = q(d, "SELECT id FROM events WHERE descr='из v1.7'")[0]["id"]
    post(M, P(f"/delete/{legacy_ev}"))
    check(q(d, "SELECT COUNT(*) c FROM events WHERE id=?", (legacy_ev,))[0]["c"] == 1, "запись без автора (из v1.7) взрослый удалить не может")
    post(A, P("/add/custom"), amount="3", note="после мамы", date=today)  # последняя запись — админа
    before = balance()
    post(M, P("/undo"))
    check(q(d, "SELECT COUNT(*) c FROM events WHERE id=?", (mom_ev["id"],))[0]["c"] == 0
          and q(d, "SELECT COUNT(*) c FROM events WHERE descr='после мамы'")[0]["c"] == 1 and balance() == before - 50,
          "«Отменить» у взрослого снимает его запись, а не последнюю чужую")
    post(M, P("/add/quick"), kind=str(dishes), date=today)
    own = q(d, "SELECT id FROM events ORDER BY id DESC LIMIT 1")[0]["id"]
    post(M, P(f"/delete/{own}"))
    check(q(d, "SELECT COUNT(*) c FROM events WHERE id=?", (own,))[0]["c"] == 0, "свою запись взрослый удаляет")

    # — 7. право на выплаты —————————————————————————————————————
    post(A, f"/users/{mom_id}", display_name="Мама", role="adult", can_payout="on", all_children="on", is_active="on")
    check("Выплата произведена" in text(M.get(HOME)), "can_payout включён: кнопка выплаты появилась")
    bal = balance()
    post(M, P("/payout"), amount="100", expected=str(bal))
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
    post(D, P("/add/quick"), kind=str(hw), date=today)
    check(q(d, "SELECT COUNT(*) c FROM events")[0]["c"] == n_ev, "без доступа к ребёнку запись невозможна")
    check(D.get("/history", follow_redirects=True).status_code == 200 and "не назначен" in text(D.get("/history", follow_redirects=True)) and D.get("/account").status_code == 200,
          "история закрыта, аккаунт доступен")
    dad_id = user_id("dad")
    post(A, f"/users/{dad_id}", display_name="Папа", role="adult", child=str(cid), is_active="on")
    check("Быстрые записи" in text(D.get(HOME)), "после назначения ребёнка доступ появился")

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
    check(M.get(HOME).status_code == 200, "отзыв чужого устройства не трогает текущее")
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
    check(A.get(HOME).status_code == 200, "администратор вошёл перед сбросом")
    load_app(d, ADMIN_PASSWORD="resetpass1", RESET_ADMIN_PASSWORD="1")
    check(redirects_to_login(A.get("/")), "RESET_ADMIN_PASSWORD=1: сессии администратора отключены")
    check(q(d, "SELECT COUNT(*) c FROM login_attempts")[0]["c"] == 0, "RESET_ADMIN_PASSWORD=1: блокировки сняты")
    check(do_login(mod.app.test_client(), "admin", "resetpass1").status_code == 302, "вход с новым паролем")


def tariff_form(mod, child_id, **over):
    """Заполненная форма страницы «Тарифы» ребёнка (как её отправил бы браузер)."""
    with mod.app.test_request_context():
        cfg = mod.child_config(mod.get_db(), child_id)
    ct = cfg["contract"]
    p1 = ct["parents"][0]
    form = {"student": cfg["student"], "limit": str(cfg["extra_payout_limit"]), "payday": str(cfg["payout_weekday"]),
            "task_idx": [], "number": ct["number"], "city": ct["city"], "cdate": ct["date"], "cend": ct["end"],
            "student_full": ct["student_full"], "student_age": str(ct["student_age"] or 0),
            "student_class": str(ct["student_class"] or 0), "p1_name": p1["name"], "p1_role": p1["role"],
            "p1_sex": "f" if p1["female"] else "m", "p2_name": "", "p2_role": "", "p2_sex": "f"}
    for g_ in cfg["grades"]:
        form[f"g{g_}"], form[f"c{g_}"] = str(cfg["grade"][g_]), str(cfg["control"][g_])
    for x in cfg["tasks"]:
        i = str(x["id"])
        form["task_idx"].append(i)
        form[f"task_{i}_id"], form[f"task_{i}_name"], form[f"task_{i}_amount"] = i, x["name"], str(x["amount"])
        if x["once_per_day"]:
            form[f"task_{i}_once"] = "on"
    form["task_idx"].append("n0")
    form.update({"task_n0_id": "", "task_n0_name": "", "task_n0_amount": ""})
    form.update(over)
    return form


def scenario_h():
    print("\n[H] несколько детей: создание, изоляция, доступы, переключатель, архив, порядок")
    d = tempfile.mkdtemp()
    mod = load_app(d, ADMIN_PASSWORD="adminpass1")
    A = mod.app.test_client()
    do_login(A, "admin", "adminpass1")
    today = date.today()
    c1 = q(d, "SELECT id FROM children")[0]["id"]
    P = lambda cid, path="": f"/c/{cid}/{path}"

    def kid(name):
        return q(d, "SELECT * FROM children WHERE name=?", (name,))[0]

    def bal(cid):
        return q(d, "SELECT COALESCE(SUM(amount),0) b FROM events WHERE child_id=? AND payout_id IS NULL", (cid,))[0]["b"]

    def n_events(cid):
        return q(d, "SELECT COUNT(*) c FROM events WHERE child_id=?", (cid,))[0]["c"]

    def order():
        return [r["id"] for r in q(d, "SELECT id FROM children ORDER BY sort_order, id")]

    def task_id(cid, name):
        return q(d, "SELECT id FROM tasks WHERE child_id=? AND name=?", (cid, name))[0]["id"]

    def new_adult(login, **opts):
        r = post(A, "/users/create", follow=True, login=login, display_name=login, role="adult", **opts)
        temp = re.search(r"Временный пароль: (\S+) —", text(r)).group(1)
        cl = mod.app.test_client()
        do_login(cl, login, temp)
        post(cl, "/account/password", current=temp, new="newpass123", repeat="newpass123")
        return cl

    # — создание ————————————————————————————————————————————————
    check(A.get("/children").status_code == 200, "/children открывается администратору")
    n = len(order())
    post(A, "/children/create", name="", copy_from="")
    post(A, "/children/create", name="максим", copy_from="")
    post(A, "/children/create", name="Х", copy_from="999")
    post(A, "/children/create", name="Х", copy_from="abc")
    check(len(order()) == n, "пустое имя, дубль имени (без учёта регистра), неверный источник копирования отклонены")
    q_exec = lambda sql, args=(): (lambda c: (c.execute(sql, args), c.commit(), c.close()))(
        sqlite3.connect(os.path.join(d, "uchet.db")))
    q_exec("UPDATE children SET payout_weekday=2, extra_payout_limit=700 WHERE id=?", (c1,))
    first_contract = q(d, "SELECT * FROM contracts WHERE child_id=?", (c1,))[0]
    first_parents = [p["name"] for p in q(d, "SELECT name FROM contract_parties WHERE contract_id=? ORDER BY sort_order",
                                          (first_contract["id"],))]

    r = post(A, "/children/create", follow=True, name="Аня", copy_from="")
    check("добавлен" in text(r), "ребёнок «Аня» добавлен")
    a = kid("Аня")["id"]
    check(q(d, "SELECT COUNT(*) c FROM tasks WHERE child_id=?", (a,))[0]["c"] == 0, "без копирования: категорий нет")
    gt = q(d, "SELECT amount FROM grade_tariffs WHERE child_id=?", (a,))
    check(len(gt) == 20 and all(r["amount"] == 0 for r in gt), "без копирования: шкала 1–10, суммы 0 ₽")
    ca = q(d, "SELECT * FROM contracts WHERE child_id=?", (a,))[0]
    check(ca["number"] == f"2/{today.year}" and ca["student_full"] == "Аня" and ca["city"] == first_contract["city"]
          and ca["student_age"] == 0, "договор создан сразу: № 2/год, ФИО = имя, город от первого ребёнка")
    check([p["name"] for p in q(d, "SELECT name FROM contract_parties WHERE contract_id=? ORDER BY sort_order", (ca["id"],))]
          == first_parents, "подписанты взяты у первого ребёнка")
    check(q(d, "SELECT COUNT(*) c FROM contract_revisions WHERE contract_id=?", (ca["id"],))[0]["c"] == 1, "первая редакция договора создана")
    check(order() == [c1, a], "новый ребёнок — в конце списка")

    post(A, "/children/create", name="Борис", copy_from=str(c1))
    b = kid("Борис")["id"]
    t1 = q(d, "SELECT name,amount,once_per_day FROM tasks WHERE child_id=? ORDER BY sort_order", (c1,))
    tb = q(d, "SELECT name,amount,once_per_day FROM tasks WHERE child_id=? ORDER BY sort_order", (b,))
    check([tuple(x) for x in t1] == [tuple(x) for x in tb] and len(tb) == 3, "копирование: категории, суммы и «раз в день» совпадают")
    check({r["id"] for r in q(d, "SELECT id FROM tasks WHERE child_id=?", (c1,))}.isdisjoint(
        {r["id"] for r in q(d, "SELECT id FROM tasks WHERE child_id=?", (b,))}), "у копии свои строки категорий (не общие)")
    cb = kid("Борис")
    check(cb["extra_payout_limit"] == 700 and cb["payout_weekday"] == 2, "копирование: лимит и день выплаты")
    check([tuple(r) for r in q(d, "SELECT type,grade,amount FROM grade_tariffs WHERE child_id=? ORDER BY type,grade", (c1,))]
          == [tuple(r) for r in q(d, "SELECT type,grade,amount FROM grade_tariffs WHERE child_id=? ORDER BY type,grade", (b,))],
          "копирование: тарифы оценок и контрольных")

    # — изоляция ———————————————————————————————————————————————
    hw1 = task_id(c1, "Домашка до 18:00")
    post(A, P(c1, "add/quick"), kind=str(hw1), date=today.isoformat())
    post(A, P(a, "add/custom"), amount="300", note="для Ани", date=today.isoformat())
    post(A, P(b, "add/quick"), kind=str(task_id(b, "Мусор")), date=today.isoformat())
    check((bal(c1), bal(a), bal(b)) == (100, 300, 50), f"балансы раздельные: {bal(c1)}/{bal(a)}/{bal(b)}")
    check("для Ани" in text(A.get(P(a, "history"))) and "для Ани" not in text(A.get(P(c1, "history"))),
          "история ребёнка не содержит чужих записей")
    check("для Ани" not in text(A.get(P(c1, "stats"))) and "Мусор" not in text(A.get(P(c1, "stats"))), "статистика раздельная")
    post(A, P(a, "payout"), amount="300", expected="300")
    check(bal(a) == 0 and bal(c1) == 100 and q(d, "SELECT COUNT(*) c FROM payouts WHERE child_id=?", (c1,))[0]["c"] == 0
          and q(d, "SELECT child_id FROM payouts")[0]["child_id"] == a, "выплата одному ребёнку не трогает остальных")
    ev_c1 = q(d, "SELECT id FROM events WHERE child_id=?", (c1,))[0]["id"]
    n = n_events(c1)
    post(A, P(a, f"delete/{ev_c1}"))
    check(n_events(c1) == n, "удаление чужой записи через адрес другого ребёнка не работает")
    post(A, P(a, "add/quick"), kind=str(hw1), date=today.isoformat())
    check(n_events(a) == 1, "категория другого ребёнка в адресе не принимается")
    check(f'action="/c/{c1}/add/quick"' in text(A.get(P(c1))) and f'action="/c/{a}/add/quick"' in text(A.get(P(a))),
          "формы привязаны к ребёнку в адресе (две вкладки не перепутаются)")
    # тарифы на ребёнка
    post(A, P(b, "tariffs"), **tariff_form(mod, b, limit="900", payday="5"))
    cb = kid("Борис")
    check(cb["extra_payout_limit"] == 900 and cb["payout_weekday"] == 5, "тарифы и день выплаты Бориса сохранены")
    c1row = q(d, "SELECT * FROM children WHERE id=?", (c1,))[0]
    check(c1row["extra_payout_limit"] == 700 and c1row["payout_weekday"] == 2, "…а у первого ребёнка не изменились")
    post(A, P(b, "tariffs"), **tariff_form(mod, b, payday="9"))
    check(kid("Борис")["payout_weekday"] == 5, "неверный день выплаты отклонён")
    form = tariff_form(mod, b)
    for k in list(form):
        if k.startswith("task_") and k.endswith("_name") and form[k] == "Мусор":
            form[k] = "Вынос мусора"
    post(A, P(b, "tariffs"), **form)
    check(q(d, "SELECT COUNT(*) c FROM tasks WHERE child_id=? AND name='Мусор'", (c1,))[0]["c"] == 1
          and q(d, "SELECT COUNT(*) c FROM events WHERE child_id=? AND descr='Мусор'", (c1,))[0]["c"] == 0
          and q(d, "SELECT COUNT(*) c FROM events WHERE child_id=? AND descr='Вынос мусора'", (b,))[0]["c"] == 1,
          "переименование категории у одного ребёнка не задевает другого")

    # — обзор и переключатель ——————————————————————————————————————
    q_exec("UPDATE children SET payout_weekday=? WHERE id=?", (today.weekday(), c1))
    html = text(A.get("/"))
    check(all(x in html for x in ("Максим", "Аня", "Борис")) and f'href="/c/{a}/"' in html, "«/» при нескольких детях — карточки всех")
    check("сегодня выплата" in html, "на карточке пометка «сегодня выплата» (день выплаты и есть накопление)")
    html = text(A.get(P(c1, "history")))
    check(f'href="/c/{a}/history"' in html and f'href="/c/{b}/history"' in html and ">Все<" in html,
          "переключатель на странице истории сохраняет раздел")
    check(f'href="/c/{a}/tariffs"' in text(A.get(P(c1, "tariffs"))), "переключатель на «Тарифах» ведёт на «Тарифы» другого ребёнка")

    # — доступы ———————————————————————————————————————————————
    lim = new_adult("lim", child=str(c1))
    allc = new_adult("allc", all_children="on")
    check(lim.get(P(c1)).status_code == 200 and lim.get(P(a)).status_code == 404 and lim.get(P(b)).status_code == 404,
          "взрослый с одним ребёнком: чужие страницы → 404")
    n = n_events(a)
    check(post(lim, P(a, "add/custom"), amount="1", note="взлом", date=today.isoformat()).status_code == 404 and n_events(a) == n,
          "…и запись чужому ребёнку (POST) → 404")
    r = lim.get("/")
    check(r.status_code == 302 and r.headers["Location"].endswith(P(c1)), "«/» ведёт сразу на единственного ребёнка")
    check(lim.get("/history").headers["Location"].endswith(P(c1, "history")), "старый адрес /history → раздел первого ребёнка")
    check('class="wrap kids"' not in text(lim.get(P(c1))), "с одним ребёнком переключателя нет")
    h = text(allc.get("/"))
    check(all(x in h for x in ("Максим", "Аня", "Борис")), "«все дети»: обзор со всеми")
    check("Недостаточно прав" in text(lim.get("/children", follow_redirects=True)), "/children закрыт для взрослого")
    post(A, "/children/create", name="Вера", copy_from="")
    v = kid("Вера")["id"]
    check(allc.get(P(v)).status_code == 200 and lim.get(P(v)).status_code == 404,
          "новый ребёнок доступен «всем детям» и не доступен ограниченному взрослому")
    lim_id = q(d, "SELECT id FROM users WHERE login='lim'")[0]["id"]
    post(A, f"/users/{lim_id}", display_name="lim", role="adult", child=[str(c1), str(a)], is_active="on")
    check(lim.get(P(a)).status_code == 200 and f'href="/c/{a}/"' in text(lim.get(P(c1))), "после назначения второго ребёнка — доступ и переключатель")
    check(A.get("/c/999/").status_code == 404, "несуществующий ребёнок → 404")

    # — архив ———————————————————————————————————————————————————
    post(A, f"/children/{b}/archive")
    check(kid("Борис")["is_archived"] == 1, "ребёнок отправлен в архив")
    r = A.get(P(b))
    check(r.status_code == 200 and "в архиве" in text(r) and "Быстрые записи" not in text(r), "админ видит архивного ребёнка: баннер, форм записи нет")
    n = n_events(b)
    post(A, P(b, "add/custom"), amount="5", note="в архив", date=today.isoformat())
    post(A, P(b, "payout"), amount="50", expected="50")
    post(A, P(b, "tariffs"), **tariff_form(mod, b, student="Переименован"))
    check(n_events(b) == n and bal(b) == 50 and kid("Борис")["id"] == b, "записи, выплата и тарифы архивного ребёнка закрыты, накопление сохранено")
    check(allc.get(P(b)).status_code == 404 and "Борис" not in text(allc.get("/")), "для взрослых архивный ребёнок скрыт (404, нет в обзоре)")
    check(f'href="/c/{b}/"' not in text(A.get(P(c1))), "в переключателе архивного нет")
    post(A, f"/children/{b}/restore")
    post(A, P(b, "add/custom"), amount="5", note="после архива", date=today.isoformat())
    check(kid("Борис")["is_archived"] == 0 and bal(b) == 55, "возврат из архива: записи снова работают")
    for cid_ in (a, b, v):
        post(A, f"/children/{cid_}/archive")
    post(A, f"/children/{c1}/archive")
    check(kid("Максим")["is_archived"] == 0, "последнего активного ребёнка архивировать нельзя")
    r = A.get("/", follow_redirects=False)
    check(r.status_code == 302 and r.headers["Location"].endswith(P(c1)), "остался один активный — «/» ведёт на него")
    for cid_ in (a, b, v):
        post(A, f"/children/{cid_}/restore")

    # — порядок, имя, старые адреса ———————————————————————————————
    check(order() == [c1, a, b, v], "порядок по умолчанию")
    post(A, f"/children/{b}/move", dir="up")
    check(order() == [c1, b, a, v], "«выше» меняет местами с соседом")
    post(A, f"/children/{c1}/move", dir="up")
    post(A, f"/children/{v}/move", dir="down")
    check(order() == [c1, b, a, v], "крайние позиции не двигаются")
    html = text(A.get(P(c1)))
    check(html.index(f'href="/c/{b}/"') < html.index(f'href="/c/{a}/"'), "переключатель идёт в заданном порядке")
    post(A, f"/children/{a}/rename", name="Анна")
    check(kid("Анна")["id"] == a and q(d, "SELECT student_full s FROM contracts WHERE child_id=?", (a,))[0]["s"] == "Аня",
          "переименование меняет имя ребёнка, а не текст договора")
    post(A, f"/children/{a}/rename", name="максим")
    check(kid("Анна")["id"] == a, "переименование в занятое имя отклонено")
    for page in ("history", "stats", "contract", "tariffs"):
        r = A.get(f"/{page}")
        check(r.status_code == 302 and r.headers["Location"].endswith(P(c1, page)), f"старый адрес /{page} → /c/{c1}/{page}")
    check(post(A, "/add/quick", kind="1", date=today.isoformat()).status_code == 404, "старый POST-адрес /add/quick больше не существует")

    # — журнал ———————————————————————————————————————————————————
    acts = {r["action"] for r in q(d, "SELECT DISTINCT action FROM audit_log WHERE child_id IS NOT NULL")}
    check({"child_create", "child_rename", "child_move", "child_archive", "child_restore"} <= acts,
          "в журнале с child_id: " + ", ".join(sorted(acts)))


def exec_sql(d, sql, args=()):
    con = sqlite3.connect(os.path.join(d, "uchet.db"))
    con.execute(sql, args)
    con.commit()
    con.close()


def cookie_hdr(r, name):
    return next((h for h in r.headers.getlist("Set-Cookie") if h.startswith(name + "=")), "")


def make_adult(mod, A, login, **opts):
    r = post(A, "/users/create", follow=True, login=login, display_name=login, role="adult", **opts)
    temp = re.search(r"Временный пароль: (\S+) —", text(r)).group(1)
    cl = mod.app.test_client()
    do_login(cl, login, temp)
    post(cl, "/account/password", current=temp, new="newpass123", repeat="newpass123")
    return cl


def scenario_i():
    print("\n[I] доступ ребёнка: ссылка, PIN, сессии, ограниченный просмотр, QR")
    d = tempfile.mkdtemp()
    mod = load_app(d, ADMIN_PASSWORD="adminpass1")
    A = mod.app.test_client()
    do_login(A, "admin", "adminpass1")
    today = date.today().isoformat()
    c1 = q(d, "SELECT id FROM children")[0]["id"]
    post(A, "/children/create", name="Аня", copy_from="")
    c2 = q(d, "SELECT id FROM children WHERE name='Аня'")[0]["id"]
    post(A, f"/c/{c1}/add/custom", amount="250", note="секрет Максима", date=today)
    post(A, f"/c/{c2}/add/custom", amount="70", note="секрет Ани", date=today)
    acc = lambda cid, tail="": f"/children/{cid}/access{tail}"

    def kid(cid):
        return q(d, "SELECT * FROM children WHERE id=?", (cid,))[0]

    def ksess(cid):
        return q(d, "SELECT * FROM sessions WHERE subject_type='child' AND subject_id=? ORDER BY id", (cid,))

    def set_mode(cid, mode, **kw):
        return post(A, acc(cid), follow=True, mode=mode, **kw)

    def gone(cl, url="/me/"):
        r = cl.get(url)
        return r.status_code == 401 and "Нужно войти" in text(r)

    # — 1. права на настройку ———————————————————————————————————
    M = make_adult(mod, A, "mom", all_children="on")
    check(A.get(acc(c1)).status_code == 200, "страница «Доступ» открывается администратору")
    check("Недостаточно прав" in text(M.get(acc(c1), follow_redirects=True)), "взрослый: страница «Доступ» закрыта")
    post(M, acc(c1), mode="link")
    check(kid(c1)["link_enabled"] == 0, "взрослый не может включить доступ ребёнка")

    # — 2. по умолчанию выключено ———————————————————————————————
    K1 = mod.app.test_client()
    check(K1.get("/k/" + "a" * 32).status_code == 404 and K1.get("/k/short").status_code == 404, "неверная ссылка → 404")
    check(gone(K1) and gone(K1, "/me/history") and gone(K1, "/me/contract"), "без сессии /me/ закрыт («Нужно войти»)")

    # — 3. режим «ссылка», QR ————————————————————————————————————
    set_mode(c1, "link")
    ch = kid(c1)
    tok1 = ch["link_token"]
    check(ch["link_enabled"] == 1 and ch["pin_enabled"] == 0 and re.fullmatch(r"[A-Za-z0-9_-]{32}", tok1),
          "включена ссылка, токен: 32 случайных URL-безопасных символа")
    html = text(A.get(acc(c1)))
    link1 = f"http://localhost/k/{tok1}"
    check(link1 in html, "на странице «Доступ» полная ссылка")
    check(str(mod.qr_svg(link1)) in html and "<svg" in html, "QR-код (SVG) на странице построен из этой ссылки")
    d2 = tempfile.mkdtemp()
    m2 = load_app(d2, ADMIN_PASSWORD="adminpass1", BASE_URL="https://kids.example.com/")
    A2 = m2.app.test_client()
    do_login(A2, "admin", "adminpass1")
    c_ = q(d2, "SELECT id FROM children")[0]["id"]
    post(A2, f"/children/{c_}/access", mode="link")
    t_ = q(d2, "SELECT link_token t FROM children")[0]["t"]
    check(f"https://kids.example.com/k/{t_}" in text(A2.get(f"/children/{c_}/access")), "BASE_URL переопределяет адрес ссылки и QR")

    # — 4. вход по ссылке, ограниченный просмотр ——————————————————
    r = K1.get("/k/" + tok1)
    sc = cookie_hdr(r, "ksid")
    check(r.status_code == 302 and r.headers["Location"].endswith("/me/") and tok1 not in r.headers["Location"],
          "вход по ссылке → редирект на /me/ (токена в адресе больше нет)")
    check("HttpOnly" in sc and "SameSite=Lax" in sc and "Max-Age=2592000" in sc, "cookie ребёнка: HttpOnly, SameSite=Lax, 30 дней")
    check(r.headers.get("Referrer-Policy") == "no-referrer", "страница входа: Referrer-Policy no-referrer")
    tokc = K1.get_cookie("ksid").value
    s1 = ksess(c1)
    check(len(s1) == 1 and s1[0]["token_hash"] == hashlib.sha256(tokc.encode()).hexdigest() and s1[0]["remember"] == 1,
          "в БД сессия ребёнка с хэшем токена (ссылка → «запомнить»)")
    check(q(d, "SELECT COUNT(*) c FROM audit_log WHERE action='kid_login' AND child_id=?", (c1,))[0]["c"] == 1, "вход ребёнка записан в журнал")
    r = K1.get("/me/")
    html = text(r)
    check(r.status_code == 200 and "секрет Максима" in html and "секрет Ани" not in html, "/me/: только данные своего ребёнка")
    check('action="/c/' not in html and "Быстрые записи" not in html and "Выплата произведена" not in html
          and 'action="/me/logout"' in html and 'href="/me/history"' in html and "Мой аккаунт" not in html,
          "/me/: ни одной формы изменения, меню ребёнка без «Аккаунт»")
    check(r.headers.get("X-Robots-Tag", "").startswith("noindex"), "заголовок X-Robots-Tag: noindex")
    h = text(K1.get("/me/history"))
    check("секрет Максима" in h and "/delete/" not in h, "/me/history: история без кнопок удаления")
    check(K1.get("/me/stats").status_code == 200, "/me/stats открывается")
    t = text(K1.get("/me/tariffs"))
    check('name="task_idx"' not in t and 'name="payday"' not in t and "Тарифы настраивают родители" in t, "/me/tariffs: только чтение")
    c = text(K1.get("/me/contract"))
    check("ДОГОВОР" in c and "Печать" in c, "/me/contract: договор и печать")

    # — 5. ребёнок не попадает на страницы взрослых ————————————————
    r = K1.get("/")
    check(r.status_code == 302 and r.headers["Location"].endswith("/me/"), "«/» у ребёнка ведёт на /me/")
    for url in (f"/c/{c1}/", f"/c/{c1}/history", f"/c/{c1}/tariffs", "/children", "/users", "/account", acc(c1)):
        check(redirects_to_login(K1.get(url)), f"ребёнок: {url} → вход (страница взрослых)")
    n_ev, n_pay = q(d, "SELECT COUNT(*) c FROM events")[0]["c"], q(d, "SELECT COUNT(*) c FROM payouts")[0]["c"]
    post(K1, f"/c/{c1}/add/custom", amount="999", note="хак", date=today)
    post(K1, f"/c/{c1}/payout", amount="250", expected="250")
    post(K1, f"/c/{c1}/undo")
    post(K1, "/children/create", name="Хакер", copy_from="")
    post(K1, acc(c1), mode="off")
    check(q(d, "SELECT COUNT(*) c FROM events")[0]["c"] == n_ev and q(d, "SELECT COUNT(*) c FROM payouts")[0]["c"] == n_pay
          and kid(c1)["link_enabled"] == 1 and q(d, "SELECT COUNT(*) c FROM children")[0]["c"] == 2,
          "ребёнок не может ни записывать, ни выплачивать, ни менять настройки")
    check(K1.post("/me/history", data={"csrf": csrf(K1)}).status_code == 405, "на страницах ребёнка нет ни одного POST (кроме выхода)")
    check("секрет Максима" in text(K1.get("/me/?child=2")) and "секрет Ани" not in text(K1.get("/me/?child=2")),
          "подмена ребёнка параметром невозможна")

    # — 6. изоляция между детьми ———————————————————————————————————
    set_mode(c2, "link")
    tok2 = kid(c2)["link_token"]
    K2 = mod.app.test_client()
    K2.get("/k/" + tok2)
    h2 = text(K2.get("/me/"))
    check("секрет Ани" in h2 and "секрет Максима" not in h2, "ребёнок 2 видит только своё")
    check("секрет Максима" in text(K1.get("/me/")), "сессия ребёнка 1 не затронута")
    Ks = mod.app.test_client()
    Ks.get("/k/" + tok1)
    Ks.get("/k/" + tok2)
    check("секрет Ани" in text(Ks.get("/me/")), "в одном браузере вход по другой ссылке переключает ребёнка")

    # — 7. взрослый и ребёнок в одном браузере независимы ——————————————
    A.get("/k/" + tok1)
    check("Быстрые записи" in text(A.get(f"/c/{c1}/")), "у админа с cookie ребёнка страницы взрослого работают как обычно")
    html = text(A.get("/me/history"))
    check("Мой аккаунт" not in html and "/delete/" not in html and 'action="/me/logout"' in html,
          "на /me/ даже у админа — режим ребёнка (без прав на изменение)")
    check(gone(M), "взрослый без сессии ребёнка: /me/ закрыт")
    post(A, "/me/logout")
    check(gone(A) and "Быстрые записи" in text(A.get(f"/c/{c1}/")), "выход ребёнка не выходит из аккаунта взрослого")

    # — 8. скользящий срок и истечение ———————————————————————————
    h = hashlib.sha256(K2.get_cookie("ksid").value.encode()).hexdigest()
    now = int(time.time())
    exec_sql(d, "UPDATE sessions SET last_seen=?, expires=? WHERE token_hash=?", (now - 7200, now + 100, h))
    r = K2.get("/me/")
    e = q(d, "SELECT expires e FROM sessions WHERE token_hash=?", (h,))[0]["e"]
    check(r.status_code == 200 and abs(e - (time.time() + 30 * 86400)) < 60 and "Max-Age=2592000" in cookie_hdr(r, "ksid"),
          "скользящее продление сессии ребёнка (срок и cookie)")
    exec_sql(d, "UPDATE sessions SET expires=? WHERE token_hash=?", (now - 5, h))
    r = K2.get("/me/")
    check(r.status_code == 401 and q(d, "SELECT COUNT(*) c FROM sessions WHERE token_hash=?", (h,))[0]["c"] == 0
          and "ksid=;" in cookie_hdr(r, "ksid"), "просроченная сессия: закрыта, удалена, cookie сброшена")

    # — 9. предпросмотр в мессенджере не создаёт сессий ———————————————
    B = mod.app.test_client()
    n = len(ksess(c2))
    for ua in ("WhatsApp/2.23.20 A", "TelegramBot (like TwitterBot)", "Mozilla/5.0 (compatible; Discordbot/2.0)"):
        r = B.get("/k/" + tok2, headers={"User-Agent": ua})
        check(r.status_code == 200 and "в браузере" in text(r) and not cookie_hdr(r, "ksid") and len(ksess(c2)) == n,
              f"бот {ua.split('/')[0].split(' ')[0]}: нейтральная страница, сессия не создана")

    # — 10. PIN ————————————————————————————————————————————————
    r = set_mode(c1, "pin")
    check("Задайте PIN" in text(r) and kid(c1)["pin_enabled"] == 0, "режим с PIN без PIN отклонён")
    for bad in ("12", "abcd", "123456789"):
        set_mode(c1, "pin", pin=bad)
    check(kid(c1)["pin_enabled"] == 0 and kid(c1)["pin_hash"] is None, "PIN короче 4, не цифры, длиннее 8 — отклонены")
    r = set_mode(c1, "pin", pin="123456")
    ch = kid(c1)
    check(ch["pin_enabled"] == 1 and ch["pin_hash"] and ch["pin_hash"] != "123456" and check_password_hash(ch["pin_hash"], "123456"),
          "PIN хранится хэшем")
    check("123456" not in text(r), "введённый вручную PIN не показывается повторно")
    check(gone(K1), "смена режима отключила устройства ребёнка")
    P = mod.app.test_client()
    r = P.get("/k/" + tok1)
    check(r.status_code == 200 and "Привет, Максим" in text(r) and not ksess(c1) and gone(P), "ссылка + PIN: форма PIN, сессии нет")
    check(redirects_to_login(P.post("/k/" + tok1, data={"pin": "123456"})) or not ksess(c1), "POST без CSRF-токена не входит")
    check(not ksess(c1), "…сессия не создана")

    def enter(cl, pin, remember=False, tok=tok1):
        cl.get("/k/" + tok)
        data = {"csrf": csrf(cl), "pin": pin}
        if remember:
            data["remember"] = "1"
        return cl.post("/k/" + tok, data=data)

    for i in range(1, 5):
        r = enter(P, "000000")
    check(kid(c1)["pin_fails"] == 4 and "Неверный PIN" in text(r) and not ksess(c1), "4 неверных PIN: счётчик 4, вход закрыт")
    r = enter(P, "000000")
    ch = kid(c1)
    check(ch["pin_locked_until"] > time.time() and "Слишком много попыток" in text(r), "5-й неверный PIN → пауза 5 минут")
    r = enter(P, "123456")
    check("Слишком много попыток" in text(r) and not ksess(c1), "во время паузы даже верный PIN не принимается")
    post(A, "/children/create", name="Вера", copy_from="")
    check(abs(kid(c1)["pin_locked_until"] - (time.time() + 300)) < 30, "пауза около 5 минут")
    set_mode(c2, "pin", pin="654321")
    P2 = mod.app.test_client()
    check(enter(P2, "654321", tok=tok2).status_code == 302, "блокировка считается на ребёнка: у другого ребёнка вход работает")
    post(A, acc(c1, "/unlock"))
    check(kid(c1)["pin_locked_until"] == 0 and kid(c1)["pin_fails"] == 0, "администратор снял блокировку")
    r = enter(P, "123456")
    sc = cookie_hdr(r, "ksid")
    s = ksess(c1)[0]
    check(r.status_code == 302 and "Max-Age" not in sc and "Expires" not in sc and s["remember"] == 0
          and abs(s["expires"] - (time.time() + 12 * 3600)) < 60, "верный PIN без «запомнить»: сеансовая cookie, срок 12 ч")
    check(P.get("/me/").status_code == 200 and kid(c1)["pin_fails"] == 0, "вход выполнен, счётчик ошибок сброшен")
    P3 = mod.app.test_client()
    check("Max-Age=2592000" in cookie_hdr(enter(P3, "123456", remember=True), "ksid"), "«запомнить это устройство»: 30 дней")
    r = set_mode(c1, "pin", gen_pin="1")
    m_ = re.search(r"PIN: (\d{6}) —", text(r))
    check(m_ and check_password_hash(kid(c1)["pin_hash"], m_.group(1)), "генерация PIN: 6 цифр, показан один раз, сохранён хэшем")
    new_pin = m_.group(1)
    check(gone(P) and gone(P3), "смена PIN отключила устройства")
    P4 = mod.app.test_client()
    check("Неверный PIN" in text(enter(P4, "123456")) or new_pin == "123456", "старый PIN больше не подходит")
    check(enter(mod.app.test_client(), new_pin).status_code == 302, "новый PIN подходит")
    set_mode(c1, "link")
    kk = mod.app.test_client()
    check(kid(c1)["pin_enabled"] == 0 and kid(c1)["pin_hash"] is not None and kk.get("/k/" + tok1).status_code == 302,
          "режим «ссылка»: PIN не спрашивают (хэш сохранён)")
    logs = " ".join(r["details"] or "" for r in q(d, "SELECT details FROM audit_log"))
    check("123456" not in logs and "654321" not in logs and new_pin not in logs, "PIN нигде не попадает в журнал")

    # — 11. перегенерация токена, выключение, архив ————————————————
    kk = mod.app.test_client()
    kk.get("/k/" + tok1)
    r = post(A, acc(c1, "/regenerate"), follow=True)
    new_tok = kid(c1)["link_token"]
    check(new_tok != tok1 and re.fullmatch(r"[A-Za-z0-9_-]{32}", new_tok), "перегенерация: новый токен")
    check(mod.app.test_client().get("/k/" + tok1).status_code == 404 and gone(kk), "старая ссылка и сессии отключены сразу")
    k_new = mod.app.test_client()
    check(k_new.get("/k/" + new_tok).status_code == 302 and k_new.get("/me/").status_code == 200, "новая ссылка работает")
    set_mode(c1, "off")
    check(kid(c1)["link_enabled"] == 0 and mod.app.test_client().get("/k/" + new_tok).status_code == 404
          and gone(k_new) and not ksess(c1), "режим «выкл»: ссылка не работает, сессии удалены")
    set_mode(c1, "link")
    check(kid(c1)["link_token"] == new_tok, "повторное включение возвращает тот же токен")
    k_arch = mod.app.test_client()
    k_arch.get("/k/" + new_tok)
    post(A, "/children/%d/archive" % c1)
    check(gone(k_arch) and mod.app.test_client().get("/k/" + new_tok).status_code == 404, "архивный ребёнок: вход и сессия закрыты")
    post(A, "/children/%d/restore" % c1)
    k_arch = mod.app.test_client()
    check(k_arch.get("/k/" + new_tok).status_code == 302, "после возврата из архива ссылка снова работает")

    # — 12. устройства ———————————————————————————————————————————
    k_b = mod.app.test_client()
    k_b.get("/k/" + new_tok)
    html = text(A.get(acc(c1)))
    sid = ksess(c1)[0]["id"]
    check(html.count("запомнено") >= 2, "на странице «Доступ» виден список устройств ребёнка")
    post(A, acc(c1, f"/sessions/{sid}/revoke"))
    check(q(d, "SELECT COUNT(*) c FROM sessions WHERE id=?", (sid,))[0]["c"] == 0, "администратор отключает одно устройство")
    post(A, acc(c1, f"/sessions/{ksess(c2)[0]['id']}/revoke")) if ksess(c2) else None
    post(A, acc(c1, "/logout-all"))
    check(not ksess(c1) and gone(k_b) and gone(k_arch), "«отключить все» устройства ребёнка")
    k_out = mod.app.test_client()
    k_out.get("/k/" + new_tok)
    k_out.get("/me/")  # как в браузере: страница отдаёт CSRF-токен
    r = post(k_out, "/me/logout")
    check(gone(k_out) and "ksid=;" in cookie_hdr(r, "ksid") and not ksess(c1), "ребёнок выходит сам")

    # — 13. журнал и списки ————————————————————————————————————
    acts = {r["action"] for r in q(d, "SELECT DISTINCT action FROM audit_log")}
    want = {"kid_login", "kid_pin_fail", "kid_pin_locked", "kid_pin_unlock", "kid_access_update", "child_token_regen",
            "kid_session_revoke", "kid_logout_all"}
    check(want <= acts, "в журнале: " + ", ".join(sorted(want)) if want <= acts else f"не хватает: {sorted(want - acts)}")
    html = text(A.get("/users"))
    check("вход ребёнка" in html and "ошибка PIN" in html and "блокировка PIN" in html, "«Последние входы» показывают входы и ошибки детей")
    html = text(A.get("/children"))
    check("ссылка" in html and "настроить" in html, "в списке детей виден режим доступа")


def scenario_j():
    print("\n[J] договор: шкала, тексты из данных, правила, редакции, права")
    d = tempfile.mkdtemp()
    mod = load_app(d, ADMIN_PASSWORD="adminpass1")
    A = mod.app.test_client()
    do_login(A, "admin", "adminpass1")
    today = date.today()
    ts = today.isoformat()
    c1 = q(d, "SELECT id FROM children")[0]["id"]
    adm = q(d, "SELECT id FROM users WHERE login='admin'")[0]["id"]
    P = lambda path="", cid=None: f"/c/{cid or c1}/{path}"

    def page(cl=A, qs="", path="contract"):
        return text(cl.get(P(path) + qs))

    def n_rev(cid=c1):
        return q(d, "SELECT COUNT(*) c FROM contract_revisions WHERE contract_id=(SELECT id FROM contracts WHERE child_id=?)", (cid,))[0]["c"]

    def revs_db(cid=c1):
        return q(d, "SELECT * FROM contract_revisions WHERE contract_id=(SELECT id FROM contracts WHERE child_id=?) ORDER BY id", (cid,))

    def statuses(cid=c1):
        with mod.app.test_request_context():
            return [r["status"] for r in mod.contract_revisions(mod.get_db(), cid)]

    def cfg_snapshot(cid=c1):
        with mod.app.test_request_context():
            con = mod.get_db()
            return mod.snapshot_from_cfg(mod.child_config(con, cid))

    def tariff(**over):
        return post(A, P("tariffs"), **tariff_form(mod, c1, **over))

    def scale():
        r = q(d, "SELECT grade_min a, grade_max b FROM children WHERE id=?", (c1,))[0]
        return r["a"], r["b"]

    def grades_in_db(typ="grade"):
        return [r["grade"] for r in q(d, "SELECT grade FROM grade_tariffs WHERE child_id=? AND type=? ORDER BY grade", (c1, typ))]

    # — 1. исходное состояние ———————————————————————————————————
    html = page()
    check(n_rev() == 1 and revs_db()[0]["created_by"] is None and json.loads(revs_db()[0]["snapshot"]).get("v") == 2,
          "у нового ребёнка одна автоматическая редакция в новом формате")
    check(scale() == (1, 10), "свежая установка: шкала в настройках ребёнка 1–10 (раньше максимум получался «9»)")
    check("ДОГОВОР № 1/2026" in html and "по шкале от 1 до 10" in html and "по пятницам" in html
          and "12-балльной" not in html, "текст: шкала и день выплаты берутся из данных (нет «12-балльной» и жёсткой «пятницы»)")
    check("оценка «10»" in html and "«10» за контрольную" in html and "оценка «9»" not in html and "— итого" in html,
          "пример собран из тарифов: высший оплачиваемый балл, без привязки к 9")
    check("Талантливый Ученик обязуется" in html and "не юридический договор" in html, "правила по умолчанию и пометка «не юридический договор»")
    check("отличаются" not in html and "Изменений с последней редакции нет" in html and "Редакции</h2>" not in html,
          "сразу после создания: нет баннера расхождения, кнопка редакции неактивна, список редакций скрыт")

    # — 2. обновление автоматических редакций со старого формата ————————
    old = {"tasks": [{"name": "Старое", "amount": 1, "once_per_day": True}], "grade": {"1": 0}, "control": {"1": 0},
           "payout_weekday": 0, "extra_payout_limit": 5, "grade_min": 1, "grade_max": 1}
    exec_sql(d, "UPDATE contract_revisions SET snapshot=? WHERE id=?", (json.dumps(old), revs_db()[0]["id"]))
    cid_ = q(d, "SELECT id FROM contracts WHERE child_id=?", (c1,))[0]["id"]
    exec_sql(d, "INSERT INTO contract_revisions(contract_id,effective_from,snapshot,created_at,created_by) VALUES(?,?,?,?,?)",
             (cid_, "2000-01-01", json.dumps(old), "2026-01-01T00:00:00", adm))
    load_app(d, ADMIN_PASSWORD="adminpass1")
    rv = revs_db()
    check(json.loads(rv[0]["snapshot"]) == cfg_snapshot() and json.loads(rv[0]["snapshot"])["v"] == 2,
          "автоматическая редакция в старом формате обновлена до текущих настроек")
    check(rv[1]["snapshot"] == json.dumps(old) and "v" not in json.loads(rv[1]["snapshot"]),
          "редакция, созданная вручную, не тронута")
    html = page(qs=f"?rev={rv[1]['id']}")
    check("Старое" in html and "ДОГОВОР № 1/2026" in html, "старый снимок открывается (данные договора подставляются из текущих)")
    exec_sql(d, "DELETE FROM contract_revisions WHERE id=?", (rv[1]["id"],))

    # — 3. шкала оценок ——————————————————————————————————————————
    tariff(gmin="1", gmax="12")
    check(scale() == (1, 12) and grades_in_db() == list(range(1, 13)) and grades_in_db("control") == list(range(1, 13)),
          "шкала расширена до 1–12 (обычные и контрольные)")
    amt = {r["grade"]: r["amount"] for r in q(d, "SELECT grade,amount FROM grade_tariffs WHERE child_id=? AND type='grade'", (c1,))}
    check(amt[10] == 200 and amt[11] == 0 and amt[12] == 0, "новые баллы — 0 ₽, прежние суммы сохранены")
    html = page(qs="?draft=1")
    check("по шкале от 1 до 12" in html, "черновик договора: шкала 1–12")
    check("по шкале от 1 до 10" in page() and "отличаются от последней редакции" in page(),
          "действующая редакция не изменилась, администратор видит баннер расхождения")
    post(A, P("add/grade"), kind="grade", grade="9", date=ts)
    post(A, P("add/grade"), kind="grade", grade="12", date=ts)
    n_ev = q(d, "SELECT COUNT(*) c FROM events")[0]["c"]
    tariff(gmin="2", gmax="5")
    check(scale() == (2, 5) and grades_in_db() == [2, 3, 4, 5] and grades_in_db("control") == [2, 3, 4, 5],
          "шкала сужена до 2–5: баллы вне диапазона убраны из тарифов")
    check(q(d, "SELECT COUNT(*) c FROM events")[0]["c"] == n_ev and q(d, "SELECT COUNT(*) c FROM events WHERE grade=9")[0]["c"] == 1,
          "ранее внесённые записи не тронуты")
    st = text(A.get(P("stats")))
    check('<span class="g">9</span>' in st and '<span class="g">12</span>' in st and '<span class="g">5</span>' in st
          and '<span class="g">1</span>' not in st, "статистика показывает баллы шкалы и все когда-либо использованные")
    post(A, P("add/grade"), kind="grade", grade="9", date=ts)
    check(q(d, "SELECT COUNT(*) c FROM events")[0]["c"] == n_ev, "балл вне новой шкалы добавить нельзя")
    for bad in ({"gmin": "5", "gmax": "5"}, {"gmin": "6", "gmax": "3"}, {"gmin": "-1", "gmax": "5"},
                {"gmin": "2", "gmax": "21"}, {"gmin": "x", "gmax": "5"}):
        tariff(**bad)
    check(scale() == (2, 5) and grades_in_db() == [2, 3, 4, 5], "от ≥ до, меньше 0, больше 20, не число — отклонены")
    tariff(gmin="2", gmax="5", g5="300", c5="900")
    html = page(qs="?draft=1")
    check("«5» за контрольную" in html and "оценка «5»" in html and "оценка «10»" not in html and "оценка «9»" not in html,
          "пример следует за шкалой: высший оплачиваемый балл — 5")

    # — 4. день выплаты и пример ——————————————————————————————————
    for day, word in (("0", "понедельникам"), ("2", "средам"), ("5", "субботам"), ("6", "воскресеньям"), ("4", "пятницам")):
        tariff(payday=day)
        check(f"по {word}" in page(qs="?draft=1"), f"день выплаты {day} → «по {word}»")
    post(A, "/children/create", name="Пустой", copy_from="")
    c2 = q(d, "SELECT id FROM children WHERE name='Пустой'")[0]["id"]
    html = text(A.get(P("contract", c2) + "?draft=1"))
    check("Пример:" not in html and "по шкале от 2 до 5" in html, "нет подходящих тарифов — примера нет; шкала скопирована у первого ребёнка")

    # — 5. правила игры и вывод данных ———————————————————————————
    html = text(A.get(P("contract/edit")))
    check("Талантливый Ученик обязуется" in html and "<textarea" in html, "форма правки: правила по умолчанию в поле")
    base_form = {"number": "1/2026", "city": "Тверь", "cdate": "2026-09-08", "cend": "2027-05-31",
                 "student_full": "Иванов Пётр Сергеевич", "student_age": "12", "student_class": "6",
                 "p1_name": "Иванов Сергей", "p1_role": "Папа", "p1_sex": "m", "p2_name": "", "p2_role": "", "p2_sex": "f"}

    def save(**over):
        data = dict(base_form, **over)
        return post(A, P("contract/edit"), **data)

    rules = "Правило <b>один</b> & <script>alert(1)</script>\n\n- пункт А\n- пункт <i>Б</i>\n\nПоследний абзац\nвторая строка"
    save(rules=rules)
    check(q(d, "SELECT rules_text r FROM contracts WHERE child_id=?", (c1,))[0]["r"] == rules, "правила сохранены")
    html = page(qs="?draft=1")
    check("&lt;script&gt;alert(1)&lt;/script&gt;" in html and "<script>alert(1)" not in html and "<b>один</b>" not in html
          and "&amp;" in html, "правила: HTML экранируется (XSS невозможен)")
    check("<ul><li>пункт А</li><li>пункт &lt;i&gt;Б&lt;/i&gt;</li></ul>" in html and "Последний абзац<br>вторая строка" in html,
          "правила: пустая строка — абзац, «- » — список, перенос строки сохраняется")
    save(rules="x" * 4001)
    check(len(q(d, "SELECT rules_text r FROM contracts WHERE child_id=?", (c1,))[0]["r"]) == len(rules), "длиннее 4000 символов — отклонено")
    save(rules="a\x00b\x07c")
    check(q(d, "SELECT rules_text r FROM contracts WHERE child_id=?", (c1,))[0]["r"] == "abc", "управляющие символы удаляются")
    save(rules="   ")
    check(q(d, "SELECT rules_text r FROM contracts WHERE child_id=?", (c1,))[0]["r"] is None
          and "Талантливый Ученик обязуется" in page(qs="?draft=1"), "пустые правила → снова текст по умолчанию")
    evil = '<img src=x onerror=alert(1)>'
    save(student_full=evil, city='"><svg onload=1>', p1_name="<u>Папа</u>", rules="")
    html = page(qs="?draft=1")
    check("&lt;img src=x onerror=alert(1)&gt;" in html and "<img src=x" not in html and "<svg onload" not in html
          and "<u>Папа</u>" not in html, "ФИО, город и подписанты в договоре экранируются")
    before = q(d, "SELECT * FROM contracts WHERE child_id=?", (c1,))[0]
    save(p1_name="", p2_name="")
    save(cdate="2026-13-45")
    save(student_age="100")
    save(student_class="13")
    after = q(d, "SELECT * FROM contracts WHERE child_id=?", (c1,))[0]
    check(tuple(before) == tuple(after), "нет подписантов / неверная дата / возраст 100 / класс 13 — ничего не сохраняется")
    save(rules="")  # вернуть нормальные данные
    check(q(d, "SELECT COUNT(*) c FROM audit_log WHERE action='contract_update' AND child_id=?", (c1,))[0]["c"] >= 1,
          "правка договора записана в журнал")
    form = tariff_form(mod, c1)
    check("student_full" not in text(A.get(P("tariffs"))) and 'name="gmin"' in text(A.get(P("tariffs")))
          and f"/c/{c1}/contract/edit" in text(A.get(P("tariffs"))), "страница «Тарифы»: данных договора нет, есть шкала и ссылка на «Договор → Править»")

    # — 6. редакции ———————————————————————————————————————————
    M = make_adult(mod, A, "mom", all_children="on")
    post(A, f"/children/{c1}/access", mode="link")
    tok = q(d, "SELECT link_token t FROM children WHERE id=?", (c1,))[0]["t"]
    K = mod.app.test_client()
    K.get("/k/" + tok)
    snap1 = revs_db()[0]["snapshot"]
    id1 = revs_db()[0]["id"]
    html_a = page()
    check("отличаются от последней редакции" in html_a and "?draft=1" in html_a, "администратор видит баннер расхождения и ссылку на черновик")
    for who, cl, url in (("взрослый", M, P("contract")), ("ребёнок", K, "/me/contract")):
        html = text(cl.get(url))
        draft = text(cl.get(url + "?draft=1"))
        check("по шкале от 1 до 10" in html and "отличаются" not in html and "Править" not in html
              and "по шкале от 1 до 10" in draft and "Черновик" not in draft, f"{who}: видит действующую редакцию, баннера и черновика нет")
    r = post(A, P("contract/revision"), follow=True, effective_from="2020-13-45")
    post(A, P("contract/revision"), effective_from=(today + timedelta(days=1000)).isoformat())
    check(n_rev() == 1, "неверная и слишком далёкая дата отклонены")
    post(M, P("contract/revision"), effective_from=ts)
    check(n_rev() == 1, "взрослый не может создать редакцию")
    post(A, P("contract/revision"), effective_from=ts)
    rv = revs_db()
    check(n_rev() == 2 and rv[1]["created_by"] == adm and json.loads(rv[1]["snapshot"]) == cfg_snapshot()
          and rv[1]["effective_from"] == ts, "редакция № 2 создана: автор, дата, снимок = текущие настройки")
    check("по шкале от 2 до 5" in page() and "по шкале от 2 до 5" in text(M.get(P("contract"))) and "по шкале от 2 до 5" in text(K.get("/me/contract")),
          "действующей стала новая редакция (для администратора, взрослого и ребёнка)")
    check("по шкале от 1 до 10" in page(qs=f"?rev={id1}") and revs_db()[0]["snapshot"] == snap1, "редакция № 1 открывается и не изменилась")
    r = post(A, P("contract/revision"), follow=True, effective_from=ts)
    check(n_rev() == 2 and "ничего не изменилось" in text(r), "дубль без изменений создать нельзя")
    check('<button class="btn sm" disabled>' in page() and "отличаются" not in page(), "кнопка неактивна, расхождения нет")
    html = text(K.get("/me/contract"))
    check("Редакции</h2>" in html and f"?rev={id1}" in html, "ребёнок видит список редакций и может открыть старую")

    tariff(payday="6", limit="777")
    post(A, P("contract/revision"), effective_from=(today + timedelta(days=10)).isoformat())
    check(n_rev() == 3 and statuses() == ["old", "current", "future"], "будущая редакция: статус «вступит в силу», действующая прежняя")
    check("по пятницам" in page() and "по воскресеньям" in page(qs=f"?rev={revs_db()[2]['id']}"), "до даты действует прежняя; будущую можно открыть")
    tariff(payday="3", limit="888")
    post(A, P("contract/revision"), effective_from=(today - timedelta(days=30)).isoformat())
    check(statuses() == ["old", "current", "future", "old"], "редакция с прошлой датой не становится действующей: " + ",".join(statuses()))
    html = page()
    check(html.count('<li><span class="d">№') == 4 and "вступит в силу" in html and "архив" in html, "список редакций: 4 строки со статусами")
    check(A.get(P("contract") + "?rev=99999").status_code == 404 and A.get(P("contract") + "?rev=abc").status_code == 404,
          "несуществующая редакция → 404")
    other = q(d, "SELECT id FROM contract_revisions WHERE contract_id=(SELECT id FROM contracts WHERE child_id=?)", (c2,))[0]["id"]
    check(A.get(P("contract") + f"?rev={other}").status_code == 404 and K.get(f"/me/contract?rev={other}").status_code == 404,
          "редакция другого ребёнка → 404")

    # — договор редактируется отдельно от редакций ————————————————
    snaps = [r["snapshot"] for r in revs_db()]
    save(number="9/2027")
    check([r["snapshot"] for r in revs_db()] == snaps and "ДОГОВОР № 1/2026" in page(), "правка данных не меняет существующие редакции")
    check("ДОГОВОР № 9/2027" in page(qs="?draft=1"), "…но видна в черновике")
    post(A, P("contract/revision"), effective_from=ts)
    check("ДОГОВОР № 9/2027" in page() and n_rev() == 5, "после новой редакции данные в действующем договоре")
    check([r["snapshot"] for r in revs_db()][:4] == snaps, "прежние редакции остались прежними")

    # — архив и права ———————————————————————————————————————————
    post(A, f"/children/{c2}/restore")
    post(A, f"/children/{c1}/archive")
    n = n_rev()
    save(number="1/1")
    post(A, P("contract/revision"), effective_from=ts)
    html = page()
    check(n_rev() == n and q(d, "SELECT number n FROM contracts WHERE child_id=?", (c1,))[0]["n"] == "9/2027"
          and "Править" not in html, "архивный ребёнок: правка и новые редакции закрыты, кнопок нет")
    post(A, f"/children/{c1}/restore")
    check("Недостаточно прав" in text(M.get(P("contract/edit"), follow_redirects=True)), "взрослый: «Договор → Править» закрыт")
    check(redirects_to_login(K.get(P("contract/edit"))) and redirects_to_login(K.post(P("contract/edit"), data={"csrf": csrf(K)})),
          "ребёнок: правка договора закрыта")
    check(q(d, "SELECT COUNT(*) c FROM audit_log WHERE action='contract_revision' AND child_id=?", (c1,))[0]["c"] == 4,
          "создание редакций записано в журнал")
    for who, cl, url in (("админ", A, P("contract")), ("взрослый", M, P("contract")), ("ребёнок", K, "/me/contract")):
        check('class="note"' in text(cl.get(url)) and "не юридический договор" in text(cl.get(url)), f"{who}: пометка внизу договора")

    # — 7. новые дети: редакция сразу совпадает с настройками ——————————
    post(A, "/children/create", name="Копия", copy_from=str(c1))
    c3 = q(d, "SELECT id FROM children WHERE name='Копия'")[0]["id"]
    for cid_, nm in ((c3, "копия"), (c2, "без копирования")):
        with mod.app.test_request_context():
            con = mod.get_db()
            last = mod.contract_revisions(con, cid_)[-1]["snapshot"]
        check(last == cfg_snapshot(cid_) and "отличаются" not in text(A.get(P("contract", cid_))),
              f"новый ребёнок ({nm}): автоматическая редакция = текущие настройки, баннера нет")
    check(json.loads(revs_db(c3)[0]["snapshot"])["payout_weekday"] == q(d, "SELECT payout_weekday p FROM children WHERE id=?", (c1,))[0]["p"],
          "копия: день выплаты в редакции совпадает с источником")


def scenario_k():
    print("\n[K] журнал действий, версия 2.0, очистка")
    # — предупреждение о коротком пароле ————————————————————————————
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        ms = load_app(tempfile.mkdtemp(), ADMIN_PASSWORD="short")
    As = ms.app.test_client()
    check("короче 8" in buf.getvalue() and do_login(As, "admin", "short").status_code == 302,
          "ADMIN_PASSWORD короче 8 символов: предупреждение в логе, но вход работает")
    check(ms.VERSION == "2.0" and "Мотиватор v2.0" in text(As.get("/", follow_redirects=True)), "версия 2.0 в приложении и подвале")

    d = tempfile.mkdtemp()
    mod = load_app(d, ADMIN_PASSWORD="adminpass1")
    A = mod.app.test_client()
    do_login(A, "admin", "adminpass1")
    today = date.today()
    ts = today.isoformat()
    c1 = q(d, "SELECT id FROM children")[0]["id"]
    P = lambda path="", cid=None: f"/c/{cid or c1}/{path}"
    secrets_list = ["adminpass1"]

    def rows(action=None, cid=None):
        sql, args = "SELECT * FROM audit_log WHERE 1=1", []
        if action:
            sql, args = sql + " AND action=?", args + [action]
        if cid:
            sql, args = sql + " AND child_id=?", args + [cid]
        return q(d, sql + " ORDER BY id", args)

    # — системные записи ————————————————————————————————————————
    sysr = [r["action"] for r in q(d, "SELECT action FROM audit_log WHERE actor='система' ORDER BY id")]
    check(sysr == ["system_migrate", "system_admin_create"], "новая установка: «система» записала создание базы и администратора: " + ",".join(sysr))
    check(q(d, "SELECT value v FROM meta WHERE key='app_version'")[0]["v"] == "2.0", "версия приложения записана в meta")

    # — деньги: записи, выплаты, удаления, отмены, тарифы ———————————————
    r = post(A, "/users/create", follow=True, login="mom", display_name="mom", role="adult", all_children="on")
    temp = re.search(r"Временный пароль: (\S+) —", text(r)).group(1)
    M = mod.app.test_client()
    do_login(M, "mom", temp)
    post(M, "/account/password", current=temp, new="newpass123", repeat="newpass123")
    secrets_list += [temp, "newpass123"]
    hw = q(d, "SELECT id FROM tasks WHERE name='Домашка до 18:00'")[0]["id"]
    dishes = q(d, "SELECT id FROM tasks WHERE name='Посудомойка'")[0]["id"]
    post(A, P("add/quick"), kind=str(hw), date=ts)
    post(A, P("add/grade"), kind="control", grade="10", date=ts)
    post(A, P("add/custom"), amount="-30", note="<b>x</b> штраф", date=ts)
    d1, d2 = rows("event_add", c1), None
    check(len(d1) == 3 and d1[0]["actor"] == "admin" and "Домашка до 18:00: +100 ₽" in d1[0]["details"]
          and "Контрольная: 10: +1000 ₽" in d1[1]["details"] and "−" not in d1[2]["details"] and ": -30 ₽" in d1[2]["details"]
          and today.strftime("%d.%m.%Y") in d1[0]["details"], "записи (дело, контрольная, своя) в журнале: автор, ребёнок, сумма, дата")
    post(A, P("payout"), amount="500", expected="999")
    check(not rows("payout"), "отклонённая выплата в журнал не попадает")
    post(A, P("payout"), amount="500", expected="1070")
    pr = rows("payout", c1)
    check(len(pr) == 1 and pr[0]["details"] == "500 ₽ из накопленных 1070 ₽; остаток 570 ₽ перенесён", "выплата: сумма, было накоплено, остаток: " + (pr[0]["details"] if pr else "—"))
    post(M, P("add/quick"), kind=str(dishes), date=ts)
    ev = q(d, "SELECT id FROM events ORDER BY id DESC LIMIT 1")[0]["id"]
    post(M, P(f"delete/{ev}"))
    dl = rows("event_delete", c1)
    check(len(dl) == 1 and dl[0]["actor"] == "mom" and "Посудомойка: +50 ₽" in dl[0]["details"] and "автор записи: mom" in dl[0]["details"],
          "удаление записи: кто удалил, что и чья запись")
    post(M, P("add/quick"), kind=str(dishes), date=ts)
    post(M, P("undo"))
    check(len(rows("event_undo", c1)) == 1 and "Посудомойка" in rows("event_undo")[0]["details"], "отмена записи в журнале")
    post(M, P(f"delete/{q(d, 'SELECT id FROM events WHERE author IS NULL OR author=1 ORDER BY id DESC LIMIT 1')[0]['id']}"))
    check(len(rows("event_delete", c1)) == 1, "запрещённое удаление в журнал не попадает")
    post(A, P("tariffs"), **tariff_form(mod, c1, limit="900"))
    tr = rows("tariffs_save", c1)
    check(len(tr) == 1 and "лимит 900 ₽" in tr[0]["details"] and "шкала 1–10" in tr[0]["details"], "сохранение тарифов в журнале: " + (tr[0]["details"] if tr else "—"))

    # — ребёнок и PIN, без секретов ————————————————————————————————
    post(A, "/children/create", name="Аня", copy_from="")
    c2 = q(d, "SELECT id FROM children WHERE name='Аня'")[0]["id"]
    post(A, P("add/custom", c2), amount="70", note="для Ани", date=ts)
    post(A, f"/children/{c1}/access", mode="link")
    tok = q(d, "SELECT link_token t FROM children WHERE id=?", (c1,))[0]["t"]
    K = mod.app.test_client()
    K.get("/k/" + tok)
    K.get("/me/")
    post(K, "/me/logout")
    kl = rows("kid_logout", c1)
    check(len(kl) == 1 and kl[0]["actor"] == "ребёнок Максим", "выход ребёнка в журнале")
    post(A, f"/children/{c1}/access", mode="pin", pin="424242")
    Pn = mod.app.test_client()
    Pn.get("/k/" + tok)
    Pn.post("/k/" + tok, data={"csrf": csrf(Pn), "pin": "000000"})
    kf = rows("kid_pin_fail", c1)
    check(len(kf) == 1 and kf[0]["details"] == "127.0.0.1", "неверный PIN: в журнале только адрес, не сам PIN")
    secrets_list += [tok, "424242", "000000"]
    blob = "\n".join(" ".join(str(v) for v in r) for r in q(d, "SELECT actor,details,action FROM audit_log"))
    check(not [x for x in secrets_list if x in blob], "в журнале нет паролей, PIN и токенов: проверено " + str(len(secrets_list)) + " значений")

    # — страница журнала ————————————————————————————————————————
    html = text(A.get("/audit"))
    check("Журнал действий" in html and 'href="/audit"' in html and "платёж" not in html, "администратор открывает /audit, пункт «Журнал» в меню")
    check("Недостаточно прав" in text(M.get("/audit", follow_redirects=True)) and 'href="/audit"' not in text(M.get(P())),
          "взрослый: /audit закрыт и пункта меню нет")
    check(redirects_to_login(mod.app.test_client().get("/audit")) and redirects_to_login(K.get("/audit")), "аноним и ребёнок: /audit закрыт")
    check("&lt;b&gt;x&lt;/b&gt; штраф" in html and "<b>x</b>" not in html, "подробности экранируются (XSS)")
    check("выплата" in html and "удаление записи" in html and "отмена записи" in html and "тарифы сохранены" in html,
          "действия показаны по-русски")
    n_arow = lambda h: h.count('class="arow"')
    body = lambda h: h[h.index("<tbody>"):h.index("</tbody>")] if "<tbody>" in h else ""
    mom_id = q(d, "SELECT id FROM users WHERE login='mom'")[0]["id"]
    h = text(A.get(f"/audit?child={c2}"))
    check("для Ани" in h and "Домашка" not in h and n_arow(h) >= 1, "фильтр по ребёнку")
    h = text(A.get(f"/audit?user={mom_id}"))
    check(">mom<" in body(h) and ">admin<" not in body(h), "фильтр по пользователю")
    h = text(A.get("/audit?user=kid"))
    check("ребёнок Максим" in body(h) and ">admin<" not in body(h) and ">mom<" not in body(h), "фильтр «дети (по ссылке)»")
    h = text(A.get("/audit?user=system"))
    check("создание/миграция базы" in body(h) and ">mom<" not in body(h) and ">admin<" not in body(h), "фильтр «система»")
    check(n_arow(text(A.get("/audit?action=payout"))) == 1, "фильтр по действию")
    check(n_arow(text(A.get(f"/audit?from={ts}&to={ts}"))) > 5 and "Записей нет" in text(A.get(f"/audit?from={(today + timedelta(days=1)).isoformat()}")),
          "фильтр по периоду")
    r = A.get("/audit?from=2026-13-45&action=<script>alert(1)</script>&child=1%20OR%201=1&user=x'&page=abc")
    check(r.status_code == 200 and "<script>alert(1)" not in text(r), "мусорные параметры фильтра игнорируются (без ошибок и инъекций)")

    # пагинация
    con = sqlite3.connect(os.path.join(d, "uchet.db"))
    con.executemany("INSERT INTO audit_log(ts,user_id,actor,child_id,action,details) VALUES(?,?,?,?,?,?)",
                    [((datetime.now() - timedelta(days=100) + timedelta(seconds=i)).isoformat(timespec="seconds"), None, "bulk", None, "login", f"bulk-{i}")
                     for i in range(120)])
    con.commit()
    con.close()
    total = q(d, "SELECT COUNT(*) c FROM audit_log")[0]["c"]
    pages = -(-total // 50)
    h1, hl = text(A.get("/audit")), text(A.get(f"/audit?page={pages}"))
    check(n_arow(h1) == 50 and f"страница 1 из {pages}" in h1 and "старше →" in h1 and "← новее" not in h1,
          f"пагинация: {total} записей → {pages} страниц по 50, на первой ссылка «старше»")
    check(n_arow(hl) == total - 50 * (pages - 1) and "← новее" in hl and "старше →" not in hl, "последняя страница: остаток и ссылка «новее»")
    check(f"страница {pages} из {pages}" in text(A.get("/audit?page=999")) and "страница 1 из" in text(A.get("/audit?page=0")),
          "номер страницы за пределами сжимается")
    cut = (today - timedelta(days=50)).isoformat()
    h = text(A.get(f"/audit?to={cut}"))
    check(n_arow(h) == 50 and "bulk-119" in h and "bulk-0<" not in h and "Домашка" not in h, "период «по …»: только старые записи, новейшие сверху")
    check("page=2" in h and f"to={cut}" in h, "ссылки пагинации сохраняют фильтры")

    # — очистка и смена версии ——————————————————————————————————
    d2 = tempfile.mkdtemp()
    m = load_app(d2, ADMIN_PASSWORD="adminpass1")
    now = datetime.now()
    ins = lambda tag, days: exec_sql(d2, "INSERT INTO audit_log(ts,user_id,actor,child_id,action,details) VALUES(?,?,?,?,?,?)",
                                     ((now - timedelta(days=days)).isoformat(timespec="seconds"), None, "t", None, "login", tag))
    tags = lambda: {r["details"] for r in q(d2, "SELECT details FROM audit_log WHERE actor='t'")}
    ins("old400", 400)
    ins("old40", 40)
    ins("fresh", 0)
    m = load_app(d2, ADMIN_PASSWORD="adminpass1")
    pr = q(d2, "SELECT details FROM audit_log WHERE action='system_prune'")
    check(tags() == {"old40", "fresh"} and len(pr) == 1 and pr[0]["details"].endswith(": 1"), "AUDIT_KEEP_DAYS по умолчанию 365: запись 400-дневной давности удалена")
    load_app(d2, ADMIN_PASSWORD="adminpass1", AUDIT_KEEP_DAYS="30")
    check(tags() == {"fresh"}, "AUDIT_KEEP_DAYS=30: удалена и 40-дневная")
    ins("old400b", 400)
    load_app(d2, ADMIN_PASSWORD="adminpass1", AUDIT_KEEP_DAYS="0")
    check("old400b" in tags(), "AUDIT_KEEP_DAYS=0: журнал не чистится")
    m = load_app(d2, ADMIN_PASSWORD="adminpass1")
    Am = m.app.test_client()
    do_login(Am, "admin", "adminpass1")
    ins("old400c", 400)
    Am.get("/audit")
    check("old400c" not in tags() and "old400b" not in tags(), "открытие страницы журнала тоже чистит старые записи")
    exec_sql(d2, "UPDATE meta SET value='2.0-dev5' WHERE key='app_version'")
    load_app(d2, ADMIN_PASSWORD="adminpass1")
    upd = q(d2, "SELECT details FROM audit_log WHERE action='system_update'")
    load_app(d2, ADMIN_PASSWORD="adminpass1")
    check(len(upd) == 1 and upd[0]["details"] == "2.0-dev5 → 2.0" and len(q(d2, "SELECT * FROM audit_log WHERE action='system_update'")) == 1,
          "смена версии записана один раз: 2.0-dev5 → 2.0")


def scenario_docs():
    print("\n[D2] документация и поставка согласованы с кодом")
    src = (ROOT / "app.py").read_text(encoding="utf-8")
    envs = set(re.findall(r'(?:os\.environ\.get|_env_int)\("([A-Z_]+)"', src))
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    xml = ET.parse(ROOT / "kidlearnpayouts.xml").getroot()
    targets = {c.get("Target") for c in xml.findall("Config")}
    check({"ADMIN_PASSWORD", "RESET_ADMIN_PASSWORD", "TZ", "SESSION_HOURS", "REMEMBER_DAYS", "COOKIE_SECURE", "TRUST_PROXY",
           "BASE_URL", "AUDIT_KEEP_DAYS", "PORT", "CONFIG_DIR"} <= envs, "найдены все переменные окружения приложения: " + ", ".join(sorted(envs)))
    check(all(e in readme for e in envs), "все переменные описаны в README")
    check(all(e in targets for e in envs - {"PORT", "CONFIG_DIR"}), "все переменные есть в шаблоне Unraid")
    check(all(e in compose for e in envs - {"PORT", "CONFIG_DIR"}), "все переменные есть в docker-compose.yml")
    check({"8080", "/config"} <= targets, "в шаблоне Unraid есть порт и папка данных")
    ver = re.search(r'image\.version="([^"]+)"', (ROOT / "Dockerfile").read_text(encoding="utf-8"))
    mod = load_app(tempfile.mkdtemp(), ADMIN_PASSWORD="adminpass1")
    check(ver and ver.group(1) == mod.VERSION == "2.0", "версия в Dockerfile совпадает с VERSION (2.0)")
    check(all(x in readme for x in ("uchet.db.bak-v1.7", "static/icons", "TRUST_PROXY", "Журнал", "Откат", "RESET_ADMIN_PASSWORD")),
          "README: обновление с 1.7, откат, иконки, прокси, журнал, сброс пароля")
    df = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    copies = [x for line in df.splitlines() if line.startswith("COPY ") for x in line.split()[1:-1]]
    check(all((ROOT / x).exists() for x in copies), "всё, что копирует Dockerfile, существует: " + ", ".join(copies))
    check("tests" in (ROOT / ".dockerignore").read_text(encoding="utf-8"), ".dockerignore исключает tests")
    used = set(re.findall(r'render_template\("([a-z_]+\.html)"', src))
    check(all((ROOT / "templates" / t).exists() for t in used), f"все шаблоны, которые вызывает app.py, существуют ({len(used)})")
    endpoints = set()
    for f in (ROOT / "templates").glob("*.html"):
        endpoints |= set(re.findall(r"url_for\('([a-z_]+)'", f.read_text(encoding="utf-8")))
    missing = {e for e in endpoints if e not in mod.app.view_functions}
    check(not missing, f"все endpoint'ы из шаблонов существуют ({len(endpoints)})" + (f", нет: {sorted(missing)}" if missing else ""))
    inline = [f.name for f in (ROOT / "templates").glob("*.html") if re.search(r"<script(?![^>]*\bsrc=)[^>]*>", f.read_text(encoding="utf-8"))]
    check(not inline, "в шаблонах нет inline-скриптов (CSP): " + (", ".join(inline) if inline else "ok"))


def scenario_l():
    print("\n[L] сквозной сценарий: v1.7 → 2.0 → роли → договор → ребёнок → журнал")
    d = tempfile.mkdtemp()
    build_v17(d, {"student": "Пётр", "tasks": TASKS_NEW, "extra_payout_limit": 800, "contract": CONTRACT,
                  "grade": GRADE, "control": CONTROL})
    mod = load_app(d, ADMIN_PASSWORD="adminpass1")
    A = mod.app.test_client()
    do_login(A, "admin", "adminpass1")
    today = date.today().isoformat()
    c1 = q(d, "SELECT id FROM children")[0]["id"]
    P = lambda path="", cid=None: f"/c/{cid or c1}/{path}"
    bal = lambda: q(d, "SELECT COALESCE(SUM(amount),0) b FROM events WHERE child_id=? AND payout_id IS NULL", (c1,))[0]["b"]

    r = A.get("/")
    check(r.status_code == 302 and r.headers["Location"].endswith(P()), "после обновления администратор попадает на единственного ребёнка")
    html = text(A.get(P()))
    check("Пётр" in html or "80" in html, "обзор показывает накопленное")
    h = text(A.get(P("history")))
    check("Прогулка с собакой" in h and "выплачено №2" in h and "из v1.7" not in h, "история v1.7 цела, выплаты на месте")
    st = text(A.get(P("stats")))
    check("Прогулка с собакой (удалена)" in st, "удалённая категория в статистике")
    ctr = text(A.get(P("contract")))
    check("Иванов Пётр Сергеевич" in ctr and "по шкале от 1 до 10" in ctr and "отличаются" not in ctr and "Тверь" in ctr,
          "договор из tariffs.json — редакция № 1 по текущим настройкам")

    r = post(A, "/users/create", follow=True, login="dad", display_name="Папа", role="adult", all_children="on", can_payout="on")
    temp = re.search(r"Временный пароль: (\S+) —", text(r)).group(1)
    D = mod.app.test_client()
    do_login(D, "dad", temp)
    post(D, "/account/password", current=temp, new="dadpass123", repeat="dadpass123")
    dishes = q(d, "SELECT id FROM tasks WHERE name='Посудомойка'")[0]["id"]
    post(D, P("add/quick"), kind=str(dishes), date=today)
    check(bal() == 130, "взрослый добавил запись: 80 + 50")
    post(D, P("payout"), amount="100", expected="130")
    check(bal() == 30 and q(d, "SELECT user_id u FROM payouts ORDER BY id DESC LIMIT 1")[0]["u"] is not None,
          "взрослый с правом выплаты отметил частичную выплату, остаток 30")
    check("Недостаточно прав" in text(D.get(P("tariffs"), follow_redirects=True)), "взрослому тарифы недоступны")

    post(A, f"/children/{c1}/access", mode="link")
    tok = q(d, "SELECT link_token t FROM children")[0]["t"]
    K = mod.app.test_client()
    K.get("/k/" + tok)
    html = text(K.get("/me/"))
    check("30" in html and 'action="/c/' not in html, "ребёнок по ссылке видит накопление и ни одной формы")
    check("Иванов Пётр Сергеевич" in text(K.get("/me/contract")), "ребёнок видит договор")
    check(redirects_to_login(K.get("/users")) and redirects_to_login(K.get(P())), "ребёнок не попадает на страницы взрослых")

    post(A, P("tariffs"), **tariff_form(mod, c1, payday="2", limit="700"))
    check("по пятницам" in text(K.get("/me/contract")), "изменение тарифов не меняет действующий договор")
    post(A, P("contract/revision"), effective_from=today)
    kc = text(K.get("/me/contract"))
    check("по средам" in kc and "Редакции</h2>" in kc, "после новой редакции ребёнок видит изменения и список редакций")

    post(A, "/children/create", name="Аня", copy_from=str(c1))
    c2 = q(d, "SELECT id FROM children WHERE name='Аня'")[0]["id"]
    post(A, P("add/custom", c2), amount="55", note="секрет Ани", date=today)
    check("секрет Ани" not in text(K.get("/me/")) and "секрет Ани" not in text(K.get("/me/history")) and redirects_to_login(K.get(P(cid=c2))),
          "ребёнок не видит данных второго ребёнка")
    check("секрет Ани" in text(A.get(P(cid=c2))) and "Аня" in text(A.get("/")), "администратор видит обоих, на «/» — обзор детей")

    h = text(A.get("/audit"))
    for label in ("создание/миграция базы", "аккаунт создан", "запись", "выплата", "вход ребёнка", "тарифы сохранены",
                  "новая редакция договора", "ребёнок добавлен"):
        check(label in h, f"журнал: «{label}»")
    sm = q(d, "SELECT details FROM audit_log WHERE action='system_migrate'")[0]["details"]
    check("v1.7 → 2.0" in sm and "uchet.db.bak-v1.7-" in sm, "журнал: миграция с указанием резервной копии")


if __name__ == "__main__":
    mig = scenario_a()
    scenario_b()
    scenario_c()
    scenario_d()
    scenario_e()
    scenario_f(mig)
    scenario_g()
    scenario_h()
    scenario_i()
    scenario_j()
    scenario_k()
    scenario_l()
    scenario_docs()
    print()
    if FAILED:
        print(f"ПРОВАЛЕНО проверок: {len(FAILED)}")
        for m in FAILED:
            print("  -", m)
        sys.exit(1)
    print("Все проверки пройдены.")
