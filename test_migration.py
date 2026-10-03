# -*- coding: utf-8 -*-
"""
Проверка миграции v1.7 → 2.0 (этап 1) на тестовой БД, созданной по схеме v1.7.
Запуск из корня репозитория:  python tests/test_migration.py
В образ не попадает (Dockerfile копирует только app.py, templates/, static/).
"""
import glob
import hashlib
import importlib.util
import itertools
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from datetime import date
from pathlib import Path

from werkzeug.security import check_password_hash

ROOT = Path(__file__).resolve().parent.parent
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
    print("\n[D] без ADMIN_PASSWORD: режим «только просмотр»")
    d = tempfile.mkdtemp()
    mod = load_app(d)
    check(q(d, "SELECT COUNT(*) c FROM users")[0]["c"] == 0, "администратор не создан")
    cl = mod.app.test_client()
    check(cl.get("/").status_code == 200 and "только просмотр" in cl.get("/").get_data(as_text=True),
          "главная открывается, показано предупреждение")


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


def scenario_f(migrated_dir):
    print("\n[F] веб-слой на мигрированной базе (вход, записи, выплата, тарифы)")
    d = tempfile.mkdtemp()
    shutil.rmtree(d)
    shutil.copytree(migrated_dir, d)
    mod = load_app(d, ADMIN_PASSWORD="other")  # пароль после сброса в сценарии A
    cl = mod.app.test_client()

    def csrf():
        with cl.session_transaction() as s:
            return s.get("csrf", "")

    def balance():
        return q(d, "SELECT COALESCE(SUM(amount),0) b FROM events WHERE payout_id IS NULL")[0]["b"]

    for url in ("/", "/history", "/stats", "/contract", "/tariffs", "/login", "/healthz", "/manifest.webmanifest"):
        check(cl.get(url).status_code == 200, f"GET {url} → 200 (аноним)")
    r = cl.post("/add/quick", data={"csrf": csrf(), "kind": "1", "date": date.today().isoformat()})
    check(balance() == 80, "аноним не может добавить запись (редирект на вход)")

    r = cl.post("/login", data={"csrf": csrf(), "password": "wrong"})
    check("Неверный пароль" in r.get_data(as_text=True), "неверный пароль отклонён")
    r = cl.post("/login", data={"csrf": csrf(), "password": "other"})
    check(r.status_code == 302, "вход по паролю администратора")
    check(cl.get("/").status_code == 200 and "Быстрые записи" in cl.get("/").get_data(as_text=True),
          "после входа видны формы записи")

    today = date.today().isoformat()
    hw = q(d, "SELECT id FROM tasks WHERE name='Домашка до 18:00'")[0]["id"]
    cl.post("/add/quick", data={"csrf": csrf(), "kind": str(hw), "date": today})
    check(balance() == 180, "быстрая запись «Домашка» +100 → 180")
    cl.post("/add/quick", data={"csrf": csrf(), "kind": str(hw), "date": today})
    check(balance() == 180, "повтор «раз в день» за ту же дату заблокирован")
    last = q(d, "SELECT * FROM events ORDER BY id DESC LIMIT 1")[0]
    check(last["type"] == "task" and last["task_id"] == hw and last["author"] is not None,
          "запись: type=task, task_id и автор сохранены")
    cl.post("/add/grade", data={"csrf": csrf(), "kind": "control", "grade": "10", "date": today})
    check(balance() == 1180, "контрольная «10» +1000 → 1180")
    cl.post("/add/grade", data={"csrf": csrf(), "kind": "student", "grade": "1", "date": today})
    check(balance() == 1180, "подделанный kind отклонён")
    cl.post("/add/custom", data={"csrf": csrf(), "amount": "-30", "note": "штраф", "date": today})
    check(balance() == 1150, "своя запись −30 → 1150")

    cl.post("/payout", data={"csrf": csrf(), "amount": "150", "expected": "1000"})
    check(balance() == 1150, "выплата с устаревшей суммой отклонена")
    cl.post("/payout", data={"csrf": csrf(), "amount": "150", "expected": "1150"})
    p = q(d, "SELECT * FROM payouts ORDER BY id DESC LIMIT 1")[0]
    check(p["amount"] == 150 and p["user_id"] is not None and balance() == 1000,
          "частичная выплата 150: сохранён user_id, остаток 1000 перенесён")
    check(q(d, "SELECT type FROM events ORDER BY id DESC LIMIT 1")[0]["type"] == "carry", "создан carry")
    cl.post("/undo", data={"csrf": csrf()})
    check(balance() == 1000, "«Отменить последнюю» не трогает carry")

    cl.post("/add/custom", data={"csrf": csrf(), "amount": "5", "note": "тест", "date": today})
    new_id = q(d, "SELECT id FROM events ORDER BY id DESC LIMIT 1")[0]["id"]
    cl.post(f"/delete/{new_id}", data={"csrf": csrf()})
    check(q(d, "SELECT COUNT(*) c FROM events WHERE id=?", (new_id,))[0]["c"] == 0, "удаление невыплаченной записи")
    cl.post("/delete/1", data={"csrf": csrf()})
    check(q(d, "SELECT COUNT(*) c FROM events WHERE id=1")[0]["c"] == 1, "выплаченную запись удалить нельзя")

    # сохранение тарифов: переименовать «Мусор», удалить «Посудомойка», добавить новую категорию
    with mod.app.test_request_context():
        cfg = mod.child_config(mod.get_db(), q(d, "SELECT id FROM children")[0]["id"])
    form = {"csrf": csrf(), "student": "Пётр", "limit": "800", "task_idx": [], "number": "1/2026", "city": "Тверь",
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
    cl.post("/tariffs", data=form)
    names = {r["name"]: r for r in q(d, "SELECT * FROM tasks")}
    check("Вынос мусора" in names and "Мусор" not in names, "категория переименована")
    check(q(d, "SELECT COUNT(*) c FROM events WHERE descr='Вынос мусора'")[0]["c"] == 1,
          "переименование обновило и старую запись")
    check(names["Посудомойка"]["is_deleted"] == 1
          and q(d, "SELECT COUNT(*) c FROM events WHERE task_id=?", (names["Посудомойка"]["id"],))[0]["c"] == 1,
          "удаление — мягкое, старая запись осталась")
    check(names["Полив цветов"]["amount"] == 40 and names["Полив цветов"]["once_per_day"] == 1, "новая категория добавлена")
    check(q(d, "SELECT name,extra_payout_limit FROM children")[0]["extra_payout_limit"] == 800, "лимит сохранён")
    check("Посудомойка (удалена)" in cl.get("/stats").get_data(as_text=True), "в статистике: «Посудомойка (удалена)»")
    check("Иванов Пётр Сергеевич" in cl.get("/contract").get_data(as_text=True), "договор показывает данные из БД")

    bad = dict(form, limit="abc")
    cl.post("/tariffs", data=bad)
    check(q(d, "SELECT extra_payout_limit l FROM children")[0]["l"] == 800, "ошибка ввода — ничего не сохраняется")
    cl.post("/logout", data={"csrf": csrf()})
    check("Быстрые записи" not in cl.get("/").get_data(as_text=True), "после выхода формы записи скрыты")


if __name__ == "__main__":
    mig = scenario_a()
    scenario_b()
    scenario_c()
    scenario_d()
    scenario_e()
    scenario_f(mig)
    print()
    if FAILED:
        print(f"ПРОВАЛЕНО проверок: {len(FAILED)}")
        for m in FAILED:
            print("  -", m)
        sys.exit(1)
    print("Все проверки пройдены.")
