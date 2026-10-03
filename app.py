# -*- coding: utf-8 -*-
"""
Мотиватор (KidLearnPayouts) — учёт по Договору — веб-версия для Unraid.

Версия 2.0, этап 1: новая схема БД (дети, пользователи, договоры, журнал…) и миграция с v1.7.
Внешне приложение работает как раньше, но уже на новой структуре и с одним ребёнком.
Просмотр и статистика пока открыты (их закроет этап 2), любые изменения — по паролю.
"""
import copy
import hmac
import json
import os
import re
import secrets
import sqlite3
import sys
import time
import traceback
from collections import defaultdict
from datetime import date, datetime, timedelta
from functools import wraps

from flask import (Flask, Response, flash, g, redirect, render_template, request,
                   send_from_directory, session, url_for)
from markupsafe import Markup
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash

VERSION = "2.0-dev1"
SCHEMA_VERSION = 1
CONFIG_DIR = os.environ.get("CONFIG_DIR", "/config")
os.makedirs(CONFIG_DIR, exist_ok=True)
DB_PATH = os.path.join(CONFIG_DIR, "uchet.db")
TARIFF_PATH = os.path.join(CONFIG_DIR, "tariffs.json")  # формат v1.7: читается только при миграции

ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
RESET_ADMIN_PASSWORD = os.environ.get("RESET_ADMIN_PASSWORD", "0") == "1"
SESSION_HOURS = int(os.environ.get("SESSION_HOURS", "12"))

if os.environ.get("TZ") and hasattr(time, "tzset"):
    time.tzset()


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def log(msg):
    print(f"[БД] {msg}", flush=True)


# ───────────────────────────── ЗНАЧЕНИЯ ПО УМОЛЧАНИЮ ─────────────────────────────
DEFAULT_TASKS = [
    {"id": "homework", "name": "Домашка до 18:00", "amount": 100, "once_per_day": True},
    {"id": "dishes", "name": "Посудомойка", "amount": 50, "once_per_day": False},
    {"id": "trash", "name": "Мусор", "amount": 50, "once_per_day": False},
]
DEFAULT_CONTRACT = {
    "number": "1/2026", "city": "", "date": "2026-09-08", "end": "2027-05-31",
    "student_full": "Якушев Максим Константинович", "student_age": 12, "student_class": 6,
    "parents": [
        {"name": "Якушев Константин Сергеевич", "role": "Папа", "female": False},
        {"name": "Якушева Наталия Сергеевна", "role": "Мама", "female": True},
    ],
}
DEFAULT_PAYOUT_WEEKDAY = 4  # пятница
DEFAULT_TARIFFS = {
    "student": "Максим",
    "tasks": DEFAULT_TASKS,
    "extra_payout_limit": 1000,
    "contract": DEFAULT_CONTRACT,
    "grade":   {"10": 200, "9": 200, "8": 100, "7": 0, "6": 0, "5": 0, "4": 0, "3": -100, "2": -100, "1": -100},
    "control": {"10": 1000, "9": 600, "8": 300, "7": 0, "6": 0, "5": 0, "4": 0, "3": -300, "2": -300, "1": -300},
}

KIND_NAMES = {
    "grade": "Оценки",
    "control": "Контрольные",
    "custom": "По договорённости",
    "carry": "Перенос остатка",
}
SPECIAL_TYPES = ("grade", "control", "custom", "carry")


# ───────────────────────────── СХЕМА БД (v2) ─────────────────────────────
SCHEMA = [
    """CREATE TABLE IF NOT EXISTS meta(
        key TEXT PRIMARY KEY, value TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS login_attempts(
        scope TEXT NOT NULL, key TEXT NOT NULL,
        fails INTEGER NOT NULL DEFAULT 0, locked_until REAL NOT NULL DEFAULT 0, updated REAL NOT NULL DEFAULT 0,
        PRIMARY KEY(scope, key))""",
    """CREATE TABLE IF NOT EXISTS users(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        login TEXT NOT NULL COLLATE NOCASE,
        display_name TEXT NOT NULL,
        role TEXT NOT NULL CHECK(role IN ('admin','adult')),
        password_hash TEXT NOT NULL,
        must_change_password INTEGER NOT NULL DEFAULT 0,
        can_payout INTEGER NOT NULL DEFAULT 0,
        all_children INTEGER NOT NULL DEFAULT 1,
        is_active INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL,
        last_login TEXT)""",
    """CREATE TABLE IF NOT EXISTS children(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        sort_order INTEGER NOT NULL DEFAULT 0,
        payout_weekday INTEGER NOT NULL DEFAULT 4,
        extra_payout_limit INTEGER NOT NULL DEFAULT 1000,
        grade_min INTEGER NOT NULL DEFAULT 1,
        grade_max INTEGER NOT NULL DEFAULT 10,
        link_enabled INTEGER NOT NULL DEFAULT 0,
        pin_enabled INTEGER NOT NULL DEFAULT 0,
        link_token TEXT,
        pin_hash TEXT,
        pin_fails INTEGER NOT NULL DEFAULT 0,
        pin_locked_until REAL NOT NULL DEFAULT 0,
        is_archived INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS user_children(
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        child_id INTEGER NOT NULL REFERENCES children(id) ON DELETE CASCADE,
        PRIMARY KEY(user_id, child_id))""",
    """CREATE TABLE IF NOT EXISTS sessions(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        token_hash TEXT NOT NULL,
        subject_type TEXT NOT NULL CHECK(subject_type IN ('user','child')),
        subject_id INTEGER NOT NULL,
        created INTEGER NOT NULL, last_seen INTEGER NOT NULL, expires INTEGER NOT NULL,
        remember INTEGER NOT NULL DEFAULT 0,
        device TEXT)""",
    """CREATE TABLE IF NOT EXISTS tasks(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        child_id INTEGER NOT NULL REFERENCES children(id),
        name TEXT NOT NULL,
        amount INTEGER NOT NULL,
        once_per_day INTEGER NOT NULL DEFAULT 0,
        sort_order INTEGER NOT NULL DEFAULT 0,
        is_deleted INTEGER NOT NULL DEFAULT 0)""",
    """CREATE TABLE IF NOT EXISTS grade_tariffs(
        child_id INTEGER NOT NULL REFERENCES children(id),
        type TEXT NOT NULL CHECK(type IN ('grade','control')),
        grade INTEGER NOT NULL,
        amount INTEGER NOT NULL,
        PRIMARY KEY(child_id, type, grade))""",
    """CREATE TABLE IF NOT EXISTS payouts(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        child_id INTEGER NOT NULL REFERENCES children(id),
        date TEXT NOT NULL,
        amount INTEGER NOT NULL,
        user_id INTEGER REFERENCES users(id))""",
    """CREATE TABLE IF NOT EXISTS events(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        child_id INTEGER NOT NULL REFERENCES children(id),
        date TEXT NOT NULL,
        type TEXT NOT NULL CHECK(type IN ('task','grade','control','custom','carry')),
        task_id INTEGER REFERENCES tasks(id),
        descr TEXT NOT NULL,
        grade INTEGER,
        amount INTEGER NOT NULL,
        payout_id INTEGER REFERENCES payouts(id),
        author INTEGER REFERENCES users(id),
        created_at TEXT)""",
    """CREATE TABLE IF NOT EXISTS contracts(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        child_id INTEGER NOT NULL REFERENCES children(id),
        number TEXT NOT NULL,
        city TEXT NOT NULL DEFAULT '',
        date TEXT NOT NULL,
        end_date TEXT NOT NULL,
        student_full TEXT NOT NULL,
        student_age INTEGER,
        student_class INTEGER,
        rules_text TEXT)""",
    """CREATE TABLE IF NOT EXISTS contract_parties(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        contract_id INTEGER NOT NULL REFERENCES contracts(id) ON DELETE CASCADE,
        name TEXT NOT NULL,
        role TEXT NOT NULL,
        female INTEGER NOT NULL DEFAULT 0,
        sort_order INTEGER NOT NULL DEFAULT 0)""",
    """CREATE TABLE IF NOT EXISTS contract_revisions(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        contract_id INTEGER NOT NULL REFERENCES contracts(id) ON DELETE CASCADE,
        effective_from TEXT NOT NULL,
        snapshot TEXT NOT NULL,
        created_at TEXT NOT NULL,
        created_by INTEGER REFERENCES users(id))""",
    """CREATE TABLE IF NOT EXISTS audit_log(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT NOT NULL,
        user_id INTEGER,
        actor TEXT,
        child_id INTEGER,
        action TEXT NOT NULL,
        details TEXT)""",
    "CREATE INDEX IF NOT EXISTS ix_events_child_payout ON events(child_id, payout_id)",
    "CREATE INDEX IF NOT EXISTS ix_events_child_date ON events(child_id, date)",
    "CREATE INDEX IF NOT EXISTS ix_payouts_child ON payouts(child_id)",
    "CREATE INDEX IF NOT EXISTS ix_tasks_child ON tasks(child_id)",
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_sessions_token ON sessions(token_hash)",
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_users_login ON users(login)",
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_children_link ON children(link_token) WHERE link_token IS NOT NULL",
]


# ───────────────────────────── ЧТЕНИЕ СТАРОГО tariffs.json (только для миграции) ─────────────────────────────
def read_legacy_tariffs():
    """Читает tariffs.json формата v1.7 (и более старого). Файл НЕ изменяется.
    Если файла нет — берутся значения по умолчанию."""
    if not os.path.exists(TARIFF_PATH):
        return copy.deepcopy(DEFAULT_TARIFFS)
    try:
        with open(TARIFF_PATH, encoding="utf-8") as f:
            t = json.load(f)
        if not isinstance(t, dict):
            raise ValueError("ожидался JSON-объект")
        merged = copy.deepcopy(DEFAULT_TARIFFS)
        merged.update(t)
        if "tasks" not in t:  # старый формат: homework / dishes / trash отдельными ключами
            merged["tasks"] = [dict(x, amount=int(t.get(x["id"], x["amount"]))) for x in DEFAULT_TASKS]
            for k in ("homework", "dishes", "trash"):
                merged.pop(k, None)
        merged["tasks"] = [
            {"id": str(x["id"]), "name": str(x["name"])[:40], "amount": int(x["amount"]),
             "once_per_day": bool(x.get("once_per_day"))} for x in merged["tasks"]]
        for k in ("grade", "control"):
            merged[k] = {int(g_): int(v) for g_, v in merged[k].items()}
        merged["student"] = str(merged["student"]).strip()[:40] or "Ребёнок"
        merged["extra_payout_limit"] = int(merged["extra_payout_limit"])
        c = copy.deepcopy(DEFAULT_CONTRACT)
        c.update(merged.get("contract") or {})
        c["parents"] = [
            {"name": str(p["name"]).strip()[:80], "role": str(p.get("role") or "Родитель")[:20],
             "female": bool(p.get("female"))}
            for p in (c.get("parents") or []) if str(p.get("name", "")).strip()][:2] or DEFAULT_CONTRACT["parents"][:1]
        for k in ("student_age", "student_class"):
            try:
                c[k] = int(c[k])
            except (TypeError, ValueError):
                c[k] = DEFAULT_CONTRACT[k]
        merged["contract"] = c
        return merged
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as e:
        raise RuntimeError(f"не удалось прочитать {TARIFF_PATH}: {e}")


# ───────────────────────────── МИГРАЦИИ ─────────────────────────────
def create_child(con, t, sort_order=0):
    """Создаёт ребёнка с тарифами, категориями и договором (+ первая редакция).
    Возвращает (child_id, {старый строковый id категории: новый числовой id})."""
    grades = sorted(t["grade"])
    cid = con.execute(
        "INSERT INTO children(name,sort_order,payout_weekday,extra_payout_limit,grade_min,grade_max,created_at)"
        " VALUES(?,?,?,?,?,?,?)",
        (t["student"], sort_order, DEFAULT_PAYOUT_WEEKDAY, t["extra_payout_limit"],
         grades[0] if grades else 1, grades[-1] if grades else 10, now_iso())).lastrowid
    task_map = {}
    for pos, x in enumerate(t["tasks"]):
        task_map[x["id"]] = con.execute(
            "INSERT INTO tasks(child_id,name,amount,once_per_day,sort_order) VALUES(?,?,?,?,?)",
            (cid, x["name"], x["amount"], int(x["once_per_day"]), pos)).lastrowid
    for typ in ("grade", "control"):
        for g_, v in t[typ].items():
            con.execute("INSERT INTO grade_tariffs(child_id,type,grade,amount) VALUES(?,?,?,?)", (cid, typ, g_, v))
    c = t["contract"]
    ctr = con.execute(
        "INSERT INTO contracts(child_id,number,city,date,end_date,student_full,student_age,student_class)"
        " VALUES(?,?,?,?,?,?,?,?)",
        (cid, c["number"], c["city"], c["date"], c["end"], c["student_full"], c["student_age"],
         c["student_class"])).lastrowid
    for pos, p in enumerate(c["parents"]):
        con.execute("INSERT INTO contract_parties(contract_id,name,role,female,sort_order) VALUES(?,?,?,?,?)",
                    (ctr, p["name"], p["role"], int(p["female"]), pos))
    snapshot = {
        "tasks": [{"name": x["name"], "amount": x["amount"], "once_per_day": x["once_per_day"]} for x in t["tasks"]],
        "grade": {str(k): v for k, v in t["grade"].items()},
        "control": {str(k): v for k, v in t["control"].items()},
        "payout_weekday": DEFAULT_PAYOUT_WEEKDAY, "extra_payout_limit": t["extra_payout_limit"],
        "grade_min": grades[0] if grades else 1, "grade_max": grades[-1] if grades else 10,
    }
    con.execute("INSERT INTO contract_revisions(contract_id,effective_from,snapshot,created_at) VALUES(?,?,?,?)",
                (ctr, c["date"], json.dumps(snapshot, ensure_ascii=False), now_iso()))
    return cid, task_map


def copy_legacy_data(con, cid, task_map):
    """Переносит events/payouts из таблиц v1.7 (events_v17, payouts_v17) и проверяет, что ничего не потерялось."""
    for p in con.execute("SELECT id,date,amount FROM payouts_v17 ORDER BY id").fetchall():
        con.execute("INSERT INTO payouts(id,child_id,date,amount,user_id) VALUES(?,?,?,?,NULL)",
                    (p["id"], cid, p["date"], p["amount"]))
    old = con.execute("SELECT id,date,kind,descr,grade,amount,payout_id FROM events_v17 ORDER BY id").fetchall()

    # категории, которых уже нет в тарифах, но на которые ссылаются события, — восстанавливаем как удалённые
    last_descr = {}
    for r in sorted(old, key=lambda r: (r["date"], r["id"])):
        last_descr[r["kind"]] = r["descr"]
    for kind, descr in last_descr.items():
        if kind not in SPECIAL_TYPES and kind not in task_map:
            task_map[kind] = con.execute(
                "INSERT INTO tasks(child_id,name,amount,once_per_day,sort_order,is_deleted) VALUES(?,?,?,?,?,1)",
                (cid, descr, 0, 0, len(task_map))).lastrowid
            log(f"категория «{descr}» (id {kind}) восстановлена как удалённая")

    for r in old:
        if r["kind"] in SPECIAL_TYPES:
            typ, task_id = r["kind"], None
        else:
            typ, task_id = "task", task_map[r["kind"]]
        con.execute(
            "INSERT INTO events(id,child_id,date,type,task_id,descr,grade,amount,payout_id,author,created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,NULL,NULL)",
            (r["id"], cid, r["date"], typ, task_id, r["descr"], r["grade"], r["amount"], r["payout_id"]))

    # контроль целостности: количество, суммы и накопление должны совпасть с v1.7
    def one(sql):
        return con.execute(sql).fetchone()[0]
    checks = [
        ("число записей", one("SELECT COUNT(*) FROM events_v17"), one("SELECT COUNT(*) FROM events")),
        ("сумма записей", one("SELECT COALESCE(SUM(amount),0) FROM events_v17"),
         one("SELECT COALESCE(SUM(amount),0) FROM events")),
        ("накопление", one("SELECT COALESCE(SUM(amount),0) FROM events_v17 WHERE payout_id IS NULL"),
         one("SELECT COALESCE(SUM(amount),0) FROM events WHERE payout_id IS NULL")),
        ("число выплат", one("SELECT COUNT(*) FROM payouts_v17"), one("SELECT COUNT(*) FROM payouts")),
        ("сумма выплат", one("SELECT COALESCE(SUM(amount),0) FROM payouts_v17"),
         one("SELECT COALESCE(SUM(amount),0) FROM payouts")),
    ]
    for what, a, b in checks:
        if a != b:
            raise RuntimeError(f"проверка после переноса не пройдена: {what} было {a}, стало {b}")
    log(f"перенесено записей: {len(old)}, выплат: {checks[3][1]}, накопление: {checks[2][1]} ₽")


def run_migrations():
    """Приводит БД к текущей схеме. Свежая установка и v1.7 обрабатываются одним путём."""
    con = sqlite3.connect(DB_PATH, timeout=10, isolation_level=None)
    con.row_factory = sqlite3.Row
    try:
        con.execute("PRAGMA journal_mode=WAL")
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "meta" in tables:
            row = con.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
            ver = int(row[0]) if row else 0
            if ver == SCHEMA_VERSION:
                return
            msg = ("база создана более новой версией приложения" if ver > SCHEMA_VERSION
                   else "неизвестная версия схемы")
            log(f"ОШИБКА: {msg} (схема {ver}, приложение ждёт {SCHEMA_VERSION}). Запуск остановлен, БД не тронута.")
            sys.exit(1)

        legacy = "events" in tables and "child_id" not in [r[1] for r in con.execute("PRAGMA table_info(events)")]
        bak = None
        try:
            if legacy:
                bak = f"{DB_PATH}.bak-v1.7-{datetime.now():%Y%m%d-%H%M%S}"
                dst = sqlite3.connect(bak)
                con.backup(dst)  # корректно и при включённом WAL
                dst.close()
                log(f"найдена база v1.7, резервная копия: {bak}")
            tariffs = read_legacy_tariffs()

            con.execute("BEGIN IMMEDIATE")
            try:
                if legacy:
                    con.execute("ALTER TABLE events RENAME TO events_v17")
                    con.execute("ALTER TABLE payouts RENAME TO payouts_v17")
                for stmt in SCHEMA:
                    con.execute(stmt)
                cid, task_map = create_child(con, tariffs)
                if legacy:
                    copy_legacy_data(con, cid, task_map)
                    con.execute("DROP TABLE events_v17")
                    con.execute("DROP TABLE payouts_v17")
                con.execute("INSERT INTO meta(key,value) VALUES('schema_version',?)", (str(SCHEMA_VERSION),))
                con.execute("INSERT INTO meta(key,value) VALUES('created_by_version',?)", (VERSION,))
                if legacy:
                    con.execute("INSERT INTO meta(key,value) VALUES('migrated_from',?)", ("1.7",))
                    con.execute("INSERT INTO meta(key,value) VALUES('migrated_at',?)", (now_iso(),))
                con.execute("COMMIT")
            except Exception:
                con.execute("ROLLBACK")
                raise
        except Exception as e:
            traceback.print_exc()
            log(f"ОШИБКА МИГРАЦИИ: {e}")
            log("Все изменения откатены, база осталась в прежнем виде."
                + (f" Резервная копия: {bak}" if bak else ""))
            sys.exit(1)
        log("миграция на схему v%d завершена" % SCHEMA_VERSION if legacy
            else "создана новая база (схема v%d)" % SCHEMA_VERSION)
    finally:
        con.close()


def ensure_admin():
    """Первый администратор создаётся из ADMIN_PASSWORD. Дальше пароль молча НЕ перезаписывается:
    для аварийного сброса нужен явный флаг RESET_ADMIN_PASSWORD=1 (вместе с ADMIN_PASSWORD)."""
    con = sqlite3.connect(DB_PATH, timeout=10, isolation_level=None)
    try:
        row = con.execute("SELECT id,password_hash FROM users WHERE role='admin' ORDER BY id LIMIT 1").fetchone()
        if row is None:
            if ADMIN_PASSWORD:
                con.execute(
                    "INSERT INTO users(login,display_name,role,password_hash,must_change_password,can_payout,"
                    "all_children,is_active,created_at) VALUES('admin','Администратор','admin',?,0,1,1,1,?)",
                    (generate_password_hash(ADMIN_PASSWORD), now_iso()))
                log("создан администратор (логин admin) из ADMIN_PASSWORD")
            else:
                log("ВНИМАНИЕ: администратора нет и ADMIN_PASSWORD не задан — доступен только просмотр.")
        elif RESET_ADMIN_PASSWORD:
            if not ADMIN_PASSWORD:
                log("RESET_ADMIN_PASSWORD=1, но ADMIN_PASSWORD пуст — пароль не изменён.")
            elif not check_password_hash(row[1], ADMIN_PASSWORD):
                con.execute("UPDATE users SET password_hash=?, is_active=1, must_change_password=0 WHERE id=?",
                            (generate_password_hash(ADMIN_PASSWORD), row[0]))
                log("пароль администратора СБРОШЕН из ADMIN_PASSWORD. Уберите RESET_ADMIN_PASSWORD после входа.")
    finally:
        con.close()


try:
    run_migrations()
    ensure_admin()
except SystemExit:
    raise
except Exception as e:  # на случай ошибок вне транзакции миграции
    traceback.print_exc()
    log(f"ОШИБКА ИНИЦИАЛИЗАЦИИ БД: {e}")
    sys.exit(1)


# ───────────────────────────── ПРИЛОЖЕНИЕ ─────────────────────────────
def _secret_key():
    path = os.path.join(CONFIG_DIR, "secret.key")
    if not os.path.exists(path):
        with open(path, "w") as f:
            f.write(secrets.token_hex(32))
        os.chmod(path, 0o600)
    with open(path) as f:
        base = f.read().strip()
    con = sqlite3.connect(DB_PATH, timeout=10)
    try:
        row = con.execute("SELECT password_hash FROM users WHERE role='admin' ORDER BY id LIMIT 1").fetchone()
    finally:
        con.close()
    # смена пароля администратора автоматически разлогинивает все сессии
    return hmac.new(base.encode(), (row[0] if row else "").encode(), "sha256").hexdigest()


app = Flask(__name__)
app.secret_key = _secret_key()
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE", "0") == "1",
    PERMANENT_SESSION_LIFETIME=timedelta(hours=SESSION_HOURS),
    MAX_CONTENT_LENGTH=64 * 1024,
)
if os.environ.get("TRUST_PROXY", "0") == "1":
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)


# ───────────────────────────── БАЗА ─────────────────────────────
def get_db():
    if "db" not in g:
        con = sqlite3.connect(DB_PATH, timeout=10, isolation_level=None)
        con.row_factory = sqlite3.Row
        g.db = con
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    con = g.pop("db", None)
    if con is not None:
        con.close()


# ───────────────────────────── НАСТРОЙКИ РЕБЁНКА (единая точка чтения из БД) ─────────────────────────────
def child_config(con, child_id):
    """Тарифы, категории и договор ребёнка одним словарём. Заменяет прежние глобальные T/TASKS/GRADES."""
    ch = con.execute("SELECT * FROM children WHERE id=?", (child_id,)).fetchone()
    if ch is None:
        return None
    tasks = [{"id": r["id"], "name": r["name"], "amount": r["amount"], "once_per_day": bool(r["once_per_day"])}
             for r in con.execute("SELECT * FROM tasks WHERE child_id=? AND is_deleted=0 ORDER BY sort_order, id",
                                  (child_id,))]
    tariff = {"grade": {}, "control": {}}
    for r in con.execute("SELECT type,grade,amount FROM grade_tariffs WHERE child_id=?", (child_id,)):
        tariff[r["type"]][r["grade"]] = r["amount"]
    crow = con.execute("SELECT * FROM contracts WHERE child_id=?", (child_id,)).fetchone()
    if crow:
        parents = [{"name": p["name"], "role": p["role"], "female": bool(p["female"])}
                   for p in con.execute("SELECT * FROM contract_parties WHERE contract_id=? ORDER BY sort_order, id",
                                        (crow["id"],))]
        contract = {
            "number": crow["number"], "city": crow["city"], "date": crow["date"], "end": crow["end_date"],
            "student_full": crow["student_full"], "student_age": crow["student_age"],
            "student_class": crow["student_class"],
            "parents": parents or copy.deepcopy(DEFAULT_CONTRACT["parents"][:1]),
        }
    else:
        contract = copy.deepcopy(DEFAULT_CONTRACT)
    return {
        "id": ch["id"], "student": ch["name"], "tasks": tasks,
        "extra_payout_limit": ch["extra_payout_limit"], "payout_weekday": ch["payout_weekday"],
        "grade": tariff["grade"], "control": tariff["control"],
        "grades": sorted(tariff["grade"], reverse=True),
        "contract": contract,
    }


def current_child():
    """Текущий ребёнок запроса. Этап 1: единственный (первый неархивный). Переключатель — этап 3."""
    if "child" not in g:
        con = get_db()
        row = (con.execute("SELECT id FROM children WHERE is_archived=0 ORDER BY sort_order, id LIMIT 1").fetchone()
               or con.execute("SELECT id FROM children ORDER BY sort_order, id LIMIT 1").fetchone())
        g.child = child_config(con, row["id"])
    return g.child


def balance(con, cid):
    return con.execute("SELECT COALESCE(SUM(amount),0) FROM events WHERE child_id=? AND payout_id IS NULL",
                       (cid,)).fetchone()[0]


def add_event(con, cid, d, etype, descr, amount, grade=None, task_id=None, author=None):
    con.execute(
        "INSERT INTO events(child_id,date,type,task_id,descr,grade,amount,author,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (cid, d, etype, task_id, descr, grade, amount, author, now_iso()))


def do_payout(con, cid, amount, expected, user_id=None):
    con.execute("BEGIN IMMEDIATE")
    try:
        bal = balance(con, cid)
        if bal != expected:
            raise ValueError("Сумма в накоплении изменилась — проверьте и повторите.")
        if not 1 <= amount <= bal:
            raise ValueError("Сумма выплаты должна быть от 1 ₽ до накопленной.")
        today = date.today().isoformat()
        pid = con.execute("INSERT INTO payouts(child_id,date,amount,user_id) VALUES(?,?,?,?)",
                          (cid, today, amount, user_id)).lastrowid
        con.execute("UPDATE events SET payout_id=? WHERE child_id=? AND payout_id IS NULL", (pid, cid))
        if amount < bal:
            add_event(con, cid, today, "carry", "Остаток после частичной выплаты", bal - amount, author=user_id)
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise


# ───────────────────────────── СТАТИСТИКА ─────────────────────────────
def monday(d):
    return d - timedelta(days=d.weekday())


def weekly_series(con, cid, weeks):
    rows = con.execute("SELECT date, amount FROM events WHERE child_id=? AND type!='carry'", (cid,)).fetchall()
    acc = defaultdict(int)
    for r in rows:
        acc[monday(datetime.strptime(r["date"], "%Y-%m-%d").date())] += r["amount"]
    start = monday(date.today())
    return [(start - timedelta(weeks=i), acc.get(start - timedelta(weeks=i), 0)) for i in range(weeks - 1, -1, -1)]


def compute_stats(con, cfg):
    cid = cfg["id"]
    rows = con.execute("SELECT date,type,task_id,grade,amount,descr FROM events "
                       "WHERE child_id=? AND type!='carry' ORDER BY date, id", (cid,)).fetchall()
    earned = sum(r["amount"] for r in rows if r["amount"] > 0)
    fines = sum(r["amount"] for r in rows if r["amount"] < 0)
    pays = con.execute("SELECT id,date,amount FROM payouts WHERE child_id=? ORDER BY id DESC", (cid,)).fetchall()
    paid = sum(p["amount"] for p in pays)
    task_info = {r["id"]: (r["name"], bool(r["is_deleted"]))
                 for r in con.execute("SELECT id,name,is_deleted FROM tasks WHERE child_id=?", (cid,))}

    per = {}  # ключ (id категории или тип) -> [кол-во, сумма, последнее название]
    dist = {"grade": defaultdict(int), "control": defaultdict(int)}
    days = set()
    for r in rows:
        key = r["task_id"] if r["type"] == "task" else r["type"]
        p = per.setdefault(key, [0, 0, ""])
        p[0] += 1
        p[1] += r["amount"]
        p[2] = r["descr"]
        if r["type"] in dist and r["grade"] is not None:
            dist[r["type"]][r["grade"]] += 1
        days.add(r["date"])

    grade_blocks = []
    for kind, title in (("grade", "Оценки"), ("control", "Контрольные")):
        cnt = sum(dist[kind].values())
        avg = sum(g_ * c for g_, c in dist[kind].items()) / cnt if cnt else None
        mx = max(dist[kind].values(), default=0)
        grade_blocks.append({
            "title": title, "count": cnt, "avg": avg,
            "bars": [(g_, dist[kind].get(g_, 0), (dist[kind].get(g_, 0) / mx * 100) if mx else 0)
                     for g_ in cfg["grades"]],
        })
    special = ("grade", "control", "custom")
    active = [x["id"] for x in cfg["tasks"]]
    order = ([k for k in active if k in per]
             + [k for k in per if k not in active and k not in special]
             + [k for k in special if k in per])

    def label(k):
        if k in special:
            return KIND_NAMES[k]
        if k in task_info and not task_info[k][1]:
            return task_info[k][0]
        return f"{task_info[k][0] if k in task_info else per[k][2]} (удалена)"

    net = earned + fines
    return {
        "earned": earned, "fines": fines, "net": net, "paid": paid,
        "balance": balance(con, cid), "pays": pays, "pay_count": len(pays),
        "pay_avg": paid // len(pays) if pays else 0,
        "cats": [(label(k), per[k][0], per[k][1]) for k in order],
        "grade_blocks": grade_blocks, "active_days": len(days),
    }


def chart_svg(data):
    W, H, top, bottom, side = 640, 230, 30, 32, 16
    vmax = max((abs(v) for _, v in data), default=0) or 1
    has_neg = any(v < 0 for _, v in data)
    zero = top + (H - top - bottom) * (0.62 if has_neg else 1.0)
    up, dn = zero - top, (H - bottom) - zero
    slot = (W - 2 * side) / len(data)
    bw = slot * 0.56
    p = [f'<svg viewBox="0 0 {W} {H}" class="chart" role="img" aria-label="Результат по неделям">',
         '<defs><linearGradient id="gp" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#5eead4"/>'
         '<stop offset="1" stop-color="#0d9488"/></linearGradient>'
         '<linearGradient id="gn" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#be123c"/>'
         '<stop offset="1" stop-color="#fb7185"/></linearGradient></defs>',
         f'<line x1="{side}" y1="{zero:.1f}" x2="{W - side}" y2="{zero:.1f}" class="axis"/>']
    for i, (wk, v) in enumerate(data):
        x = side + i * slot + (slot - bw) / 2
        cx = x + bw / 2
        if v >= 0:
            h = max(up * v / vmax, 2 if v else 0)
            p.append(f'<rect x="{x:.1f}" y="{zero - h:.1f}" width="{bw:.1f}" height="{h:.1f}" rx="5" fill="url(#gp)"/>')
            ty = zero - h - 7
        else:
            h = max(dn * -v / vmax, 2)
            p.append(f'<rect x="{x:.1f}" y="{zero:.1f}" width="{bw:.1f}" height="{h:.1f}" rx="5" fill="url(#gn)"/>')
            ty = zero + h + 14
        if v:
            p.append(f'<text x="{cx:.1f}" y="{ty:.1f}" class="val" text-anchor="middle">{v}</text>')
        p.append(f'<text x="{cx:.1f}" y="{H - 10}" class="lbl" text-anchor="middle">{wk.strftime("%d.%m")}</text>')
    p.append("</svg>")
    return Markup("".join(p))


WEEKDAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]


def pay_hint(weekday):
    today = date.today()
    days = (weekday - today.weekday()) % 7
    if days == 0:
        return f"Сегодня {WEEKDAYS[weekday]} — день выплаты"
    return f"Ближайшая выплата — {WEEKDAYS[weekday]}, {(today + timedelta(days=days)).strftime('%d.%m.%Y')}"


# ───────────────────────────── АВТОРИЗАЦИЯ (этап 1: один пароль администратора) ─────────────────────────────
FAILS = {}  # ip -> [count, locked_until]  (в БД переедет на этапе 2)


def client_locked(ip):
    rec = FAILS.get(ip)
    return bool(rec and rec[1] > time.time())


def register_fail(ip):
    rec = FAILS.setdefault(ip, [0, 0])
    rec[0] += 1
    if rec[0] >= 5:
        rec[0], rec[1] = 0, time.time() + 300
    time.sleep(0.7)  # притормаживаем перебор


def get_admin():
    if "admin_row" not in g:
        g.admin_row = get_db().execute(
            "SELECT id,password_hash FROM users WHERE role='admin' AND is_active=1 ORDER BY id LIMIT 1").fetchone()
    return g.admin_row


def is_admin():
    return session.get("admin") is True and session.get("uid") is not None


def admin_required(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        if not is_admin():
            flash("Для этого действия нужен пароль.", "err")
            return redirect(url_for("login"))
        return fn(*a, **kw)
    return wrapper


@app.before_request
def csrf_protect():
    if request.method == "POST":
        sent = request.form.get("csrf", "").encode()
        tok = session.get("csrf", "").encode()
        if not tok or not hmac.compare_digest(sent, tok):
            flash("Сессия устарела, обновите страницу и повторите.", "err")
            return redirect(request.referrer or url_for("index"))


@app.context_processor
def inject():
    if "csrf" not in session:
        session["csrf"] = secrets.token_hex(16)
    return {
        "csrf": session["csrf"], "is_admin": is_admin(), "student": current_child()["student"],
        "read_only_mode": get_admin() is None, "version": VERSION,
    }


@app.after_request
def headers(resp):
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "same-origin"
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
        "script-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'")
    if request.endpoint not in ("static", "apple_icon", "manifest"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


@app.template_filter("money")
def money(n, sign=False):
    n = int(n)
    s = f"{abs(n):,}".replace(",", "\u202f")
    pre = "−" if n < 0 else ("+" if sign and n > 0 else "")
    return f"{pre}{s}\u00a0₽"


@app.template_filter("dmy")
def dmy(iso):
    return datetime.strptime(iso, "%Y-%m-%d").strftime("%d.%m.%Y")


DOW = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]


@app.template_filter("dow")
def dow(iso):
    return DOW[datetime.strptime(iso, "%Y-%m-%d").weekday()]


# ───────────────────────────── СТРАНИЦЫ (пока открытые) ─────────────────────────────
@app.route("/")
def index():
    con = get_db()
    cfg = current_child()
    cid = cfg["id"]
    bal = balance(con, cid)
    limit = cfg["extra_payout_limit"]
    recent = con.execute("SELECT * FROM events WHERE child_id=? ORDER BY date DESC, id DESC LIMIT 8", (cid,)).fetchall()
    return render_template(
        "index.html", bal=bal, limit=limit, progress=max(0, min(100, bal * 100 // limit)) if limit else 0,
        over=bal > limit, hint=pay_hint(cfg["payout_weekday"]), recent=recent,
        chart=chart_svg(weekly_series(con, cid, 8)),
        today=date.today().isoformat(), tasks=cfg["tasks"], grades=cfg["grades"],
        tariff_json=json.dumps({k: cfg[k] for k in ("grade", "control")}))


@app.route("/history")
def history():
    con = get_db()
    cid = current_child()["id"]
    events = con.execute("SELECT * FROM events WHERE child_id=? ORDER BY date DESC, id DESC LIMIT 500",
                         (cid,)).fetchall()
    pays = con.execute("SELECT * FROM payouts WHERE child_id=? ORDER BY id DESC LIMIT 100", (cid,)).fetchall()
    return render_template("history.html", events=events, pays=pays)


@app.route("/stats")
def stats():
    con = get_db()
    cfg = current_child()
    return render_template("stats.html", s=compute_stats(con, cfg), chart=chart_svg(weekly_series(con, cfg["id"], 12)))


# ───────────────────────────── ИКОНКИ И МАНИФЕСТ (для «На экран Домой») ─────────────────────────────
@app.route("/apple-touch-icon.png")
@app.route("/apple-touch-icon-precomposed.png")
def apple_icon():
    return send_from_directory(os.path.join(app.root_path, "static", "icons"),
                               "apple-touch-icon.png", max_age=86400)


@app.route("/manifest.webmanifest")
def manifest():
    data = {
        "name": "Мотиватор", "short_name": "Мотиватор", "lang": "ru",
        "start_url": "/", "scope": "/", "display": "standalone",
        "background_color": "#0a0e16", "theme_color": "#0a0e16",
        "icons": [
            {"src": "/static/icons/icon-192.png", "sizes": "192x192", "type": "image/png"},
            {"src": "/static/icons/icon-512.png", "sizes": "512x512", "type": "image/png"},
        ],
    }
    return Response(json.dumps(data, ensure_ascii=False), mimetype="application/manifest+json")


@app.route("/healthz")
def healthz():
    return "ok"


# ───────────────────────────── ДОГОВОР ─────────────────────────────
MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля",
          "августа", "сентября", "октября", "ноября", "декабря"]


@app.template_filter("rudate")
def rudate(iso):
    d = datetime.strptime(iso, "%Y-%m-%d")
    return f"«{d.day:02d}» {MONTHS[d.month - 1]} {d.year} г."


@app.template_filter("fio")
def fio(full):
    p = full.split()
    return " ".join([p[0]] + [x[0] + "." for x in p[1:]]) if p else ""


def grade_columns(t):
    """Склеивает подряд идущие баллы с одинаковыми суммами: [(метка, оценка, контрольная), ...]"""
    groups = []
    for g_ in sorted(t["grade"], reverse=True):
        key = (t["grade"][g_], t["control"][g_])
        if groups and groups[-1][3] == key:
            groups[-1][1] = g_
        else:
            groups.append([g_, g_, key[0], key])
    return [(f"«{hi}»" if hi == lo else f"{lo} – {hi}", k[0], k[1]) for hi, lo, _, k in groups]


def contract_example(t):
    parts = []
    once = next((x for x in t["tasks"] if x["once_per_day"] and x["amount"] > 0), None)
    chore = next((x for x in t["tasks"] if not x["once_per_day"] and x["amount"] > 0), None)
    for x in (once, chore):
        if x:
            parts.append((f"«{x['name']}»", x["amount"]))
    if t["grade"].get(9):
        parts.append(("оценка «9»", t["grade"][9]))
    if t["control"].get(10):
        parts.append(("«10» за контрольную", t["control"][10]))
    if len(parts) < 2:
        return None
    total = sum(a for _, a in parts)
    return ", ".join(f"{n} ({money(a, True)})" for n, a in parts) + f" — итого {money(total)}"


@app.route("/contract")
def contract():
    cfg = current_child()
    return render_template("contract.html", c=cfg["contract"], tasks=cfg["tasks"], limit=cfg["extra_payout_limit"],
                           cols=grade_columns(cfg), example=contract_example(cfg))


# ───────────────────────────── ТАРИФЫ ─────────────────────────────
@app.route("/tariffs")
def tariffs():
    cfg = current_child()
    return render_template("tariffs.html", t=cfg, grades=cfg["grades"])


class FormError(ValueError):
    """Ошибка ввода с понятным пользователю текстом."""


def parse_contract(f, to_int):
    def d(name, what):
        try:
            return datetime.strptime(f.get(name, ""), "%Y-%m-%d").date().isoformat()
        except ValueError:
            raise FormError(f"{what}: некорректная дата.")
    parents = []
    for i in (1, 2):
        name = " ".join(f.get(f"p{i}_name", "").split())[:80]
        if name:
            parents.append({"name": name, "role": f.get(f"p{i}_role", "").strip()[:20] or "Родитель",
                            "female": f.get(f"p{i}_sex") == "f"})
    if not parents:
        raise FormError("В договоре должен быть хотя бы один родитель.")
    full = " ".join(f.get("student_full", "").split())[:80]
    if not full:
        raise FormError("Укажите полное имя ребёнка для договора.")
    return {
        "number": f.get("number", "").strip()[:20] or "1/2026",
        "city": f.get("city", "").strip()[:40],
        "date": d("cdate", "Дата договора"), "end": d("cend", "Срок действия"),
        "student_full": full,
        "student_age": to_int(f.get("student_age", ""), 0, 99, "Возраст"),
        "student_class": to_int(f.get("student_class", ""), 0, 12, "Класс"),
        "parents": parents,
    }


@app.route("/tariffs", methods=["POST"], endpoint="tariffs_save")
@admin_required
def tariffs_save():
    f = request.form
    cfg = current_child()
    cid = cfg["id"]
    existing = {x["id"]: x for x in cfg["tasks"]}

    def to_int(raw, lo, hi, what):
        try:
            v = int(str(raw).replace("−", "-").replace(" ", "").replace("\u00a0", ""))
        except ValueError:
            raise FormError(f"{what}: нужно целое число.")
        if not lo <= v <= hi:
            raise FormError(f"{what}: допустимо от {lo} до {hi}.")
        return v

    def parse_tasks():
        if not f.getlist("task_idx"):
            raise FormError("Форма категорий повреждена — обновите страницу.")
        ops, deleted, names = [], [], set()
        for idx in dict.fromkeys(f.getlist("task_idx")):
            if not re.fullmatch(r"\w{1,24}", idx):
                raise FormError("Форма категорий повреждена — обновите страницу.")
            tid_raw = f.get(f"task_{idx}_id", "").strip()
            name = f.get(f"task_{idx}_name", "").strip()[:40]
            raw = f.get(f"task_{idx}_amount", "").strip()
            tid = None
            if tid_raw:
                if not re.fullmatch(r"[0-9]{1,9}", tid_raw) or int(tid_raw) not in existing:
                    raise FormError("Категория не найдена — обновите страницу.")
                tid = int(tid_raw)
                if f.get(f"task_{idx}_del") is not None:
                    deleted.append(tid)  # мягкое удаление: старые записи остаются в истории
                    continue
            elif not name and not raw:
                continue  # пустая строка «добавить»
            if not name:
                raise FormError("У категории должно быть название.")
            if name.casefold() in names:
                raise FormError(f"Категория «{name}» указана дважды.")
            names.add(name.casefold())
            amount = to_int(raw, -100000, 100000, f"«{name}»")
            ops.append({"id": tid, "name": name, "amount": amount, "once": f.get(f"task_{idx}_once") is not None})
        if len(ops) > 30:
            raise FormError("Слишком много категорий (максимум 30).")
        return ops, deleted

    try:
        student = f.get("student", "").strip()[:40]
        if not student:
            raise FormError("Укажите имя ученика.")
        ops, deleted = parse_tasks()
        limit = to_int(f.get("limit", ""), 0, 100000, "Выплата вне очереди")
        grade = {g_: to_int(f.get(f"g{g_}", ""), -100000, 100000, f"Оценка {g_}") for g_ in cfg["grades"]}
        control = {g_: to_int(f.get(f"c{g_}", ""), -100000, 100000, f"Контрольная {g_}") for g_ in cfg["grades"]}
        ctr = parse_contract(f, to_int)
    except FormError as e:
        flash(str(e), "err")
        return redirect(url_for("tariffs"))

    con = get_db()
    con.execute("BEGIN IMMEDIATE")  # всё сохраняется одной транзакцией — без «половинчатых» тарифов
    try:
        for tid in deleted:
            con.execute("UPDATE tasks SET is_deleted=1 WHERE id=? AND child_id=?", (tid, cid))
        for pos, x in enumerate(ops):
            if x["id"]:
                con.execute("UPDATE tasks SET name=?, amount=?, once_per_day=?, sort_order=? WHERE id=? AND child_id=?",
                            (x["name"], x["amount"], int(x["once"]), pos, x["id"], cid))
                if existing[x["id"]]["name"] != x["name"]:  # переименование обновляет и старые записи
                    con.execute("UPDATE events SET descr=? WHERE child_id=? AND task_id=?", (x["name"], cid, x["id"]))
            else:
                con.execute("INSERT INTO tasks(child_id,name,amount,once_per_day,sort_order) VALUES(?,?,?,?,?)",
                            (cid, x["name"], x["amount"], int(x["once"]), pos))
        con.execute("UPDATE children SET name=?, extra_payout_limit=? WHERE id=?", (student, limit, cid))
        for typ, vals in (("grade", grade), ("control", control)):
            for g_, v in vals.items():
                con.execute("INSERT OR REPLACE INTO grade_tariffs(child_id,type,grade,amount) VALUES(?,?,?,?)",
                            (cid, typ, g_, v))
        crow = con.execute("SELECT id FROM contracts WHERE child_id=?", (cid,)).fetchone()
        con.execute(
            "UPDATE contracts SET number=?, city=?, date=?, end_date=?, student_full=?, student_age=?, student_class=?"
            " WHERE id=?",
            (ctr["number"], ctr["city"], ctr["date"], ctr["end"], ctr["student_full"], ctr["student_age"],
             ctr["student_class"], crow["id"]))
        con.execute("DELETE FROM contract_parties WHERE contract_id=?", (crow["id"],))
        for pos, p in enumerate(ctr["parents"]):
            con.execute("INSERT INTO contract_parties(contract_id,name,role,female,sort_order) VALUES(?,?,?,?,?)",
                        (crow["id"], p["name"], p["role"], int(p["female"]), pos))
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    flash("Тарифы сохранены. Новые суммы действуют для будущих записей.", "ok")
    return redirect(url_for("tariffs"))


# ───────────────────────────── ВХОД / ВЫХОД ─────────────────────────────
@app.route("/login", methods=["GET", "POST"])
def login():
    if is_admin():
        return redirect(url_for("index"))
    if request.method == "POST":
        ip = request.remote_addr or "?"
        adm = get_admin()
        if adm is None:
            flash("Пароль не задан в настройках контейнера (ADMIN_PASSWORD).", "err")
        elif client_locked(ip):
            flash("Слишком много попыток. Подождите 5 минут.", "err")
        elif check_password_hash(adm["password_hash"], request.form.get("password", "")):
            FAILS.pop(ip, None)
            session.clear()
            session["admin"] = True
            session["uid"] = adm["id"]
            session["csrf"] = secrets.token_hex(16)
            session.permanent = True
            get_db().execute("UPDATE users SET last_login=? WHERE id=?", (now_iso(), adm["id"]))
            return redirect(url_for("index"))
        else:
            register_fail(ip)
            flash("Неверный пароль.", "err")
    return render_template("login.html")


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("index"))


# ───────────────────────────── ИЗМЕНЕНИЯ (только с паролем) ─────────────────────────────
def parse_date(raw):
    d = datetime.strptime(raw, "%Y-%m-%d").date()
    if abs((d - date.today()).days) > 400:
        raise ValueError
    return d.isoformat()


def back():
    return redirect(request.referrer if request.referrer and request.referrer.startswith(request.host_url)
                    else url_for("index"))


@app.route("/add/quick", methods=["POST"])
@admin_required
def add_quick():
    cfg = current_child()
    try:
        d = parse_date(request.form.get("date", ""))
    except ValueError:
        flash("Некорректная дата.", "err")
        return back()
    raw = request.form.get("kind", "")
    task = next((x for x in cfg["tasks"] if raw.isdigit() and x["id"] == int(raw)), None)
    if not task:
        flash("Такой категории нет (возможно, её удалили) — обновите страницу.", "err")
        return back()
    con = get_db()
    if task["once_per_day"] and con.execute(
            "SELECT 1 FROM events WHERE child_id=? AND date=? AND task_id=?", (cfg["id"], d, task["id"])).fetchone():
        flash(f"«{task['name']}»: за эту дату уже записано.", "err")
        return back()
    add_event(con, cfg["id"], d, "task", task["name"], task["amount"], task_id=task["id"], author=session.get("uid"))
    flash(f"{task['name']}: {money(task['amount'], True)}", "ok")
    return back()


@app.route("/add/grade", methods=["POST"])
@admin_required
def add_grade():
    cfg = current_child()
    kind = request.form.get("kind", "")
    try:
        if kind not in ("grade", "control"):
            raise KeyError(kind)
        d = parse_date(request.form.get("date", ""))
        grade = int(request.form.get("grade", ""))
        amount = cfg[kind][grade]
    except (ValueError, KeyError):
        flash("Некорректные данные оценки.", "err")
        return back()
    descr = f"{'Контрольная' if kind == 'control' else 'Оценка'}: {grade}"
    add_event(get_db(), cfg["id"], d, kind, descr, amount, grade=grade, author=session.get("uid"))
    flash(f"{descr} → {money(amount, True)}", "ok")
    return back()


@app.route("/add/custom", methods=["POST"])
@admin_required
def add_custom():
    try:
        d = parse_date(request.form.get("date", ""))
        amount = int(request.form.get("amount", "").replace("−", "-").replace(" ", ""))
        if amount == 0 or abs(amount) > 100000:
            raise ValueError
    except ValueError:
        flash("Введите сумму целым числом (можно с минусом) и корректную дату.", "err")
        return back()
    note = request.form.get("note", "").strip()[:120] or "По договорённости"
    add_event(get_db(), current_child()["id"], d, "custom", note, amount, author=session.get("uid"))
    flash(f"{note}: {money(amount, True)}", "ok")
    return back()


@app.route("/payout", methods=["POST"])
@admin_required
def payout():
    try:
        amount = int(request.form.get("amount", ""))
        expected = int(request.form.get("expected", ""))
        do_payout(get_db(), current_child()["id"], amount, expected, session.get("uid"))
    except ValueError as e:
        flash(str(e) if str(e) and "invalid literal" not in str(e) else "Некорректная сумма.", "err")
    else:
        flash(f"Выплата отмечена: {money(amount)}", "ok")
    return redirect(url_for("index"))


@app.route("/delete/<int:event_id>", methods=["POST"])
@admin_required
def delete_event(event_id):
    cur = get_db().execute(
        "DELETE FROM events WHERE id=? AND child_id=? AND payout_id IS NULL AND type!='carry'",
        (event_id, current_child()["id"]))
    flash("Запись удалена." if cur.rowcount else "Эту запись удалить нельзя (уже выплачена или служебная).",
          "ok" if cur.rowcount else "err")
    return back()


@app.route("/undo", methods=["POST"])
@admin_required
def undo():
    con = get_db()
    row = con.execute(
        "SELECT id,type FROM events WHERE child_id=? AND payout_id IS NULL ORDER BY id DESC LIMIT 1",
        (current_child()["id"],)).fetchone()
    if not row or row["type"] == "carry":
        flash("Отменять нечего.", "err")
    else:
        con.execute("DELETE FROM events WHERE id=?", (row["id"],))
        flash("Последняя запись отменена.", "ok")
    return back()


if __name__ == "__main__":
    from waitress import serve
    port = int(os.environ.get("PORT", "8080"))
    print(f"Мотиватор v{VERSION} запущен на порту {port}", flush=True)
    serve(app, host="0.0.0.0", port=port, threads=6)
