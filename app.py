# -*- coding: utf-8 -*-
"""
Мотиватор (KidLearnPayouts) — учёт по Договору — веб-версия для Unraid. Версия 2.0.

Несколько детей (у каждого свои баланс, записи, выплаты, тарифы, категории и договор), роли «Администратор» /
«Взрослый» / «Ребёнок», серверные сессии и «Запомнить меня», доступ ребёнка по секретной ссылке и PIN,
договор с редакциями, журнал действий. Одна база SQLite на одну семью; миграция с v1.7 — автоматическая.
Подробности — в README.md.
"""
import copy
import hashlib
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

from flask import (Flask, Response, abort, flash, g, redirect, render_template, request,
                   send_from_directory, session, url_for)
import segno
from markupsafe import Markup, escape
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash

VERSION = "2.0"
SCHEMA_VERSION = 1
CONFIG_DIR = os.environ.get("CONFIG_DIR", "/config")
os.makedirs(CONFIG_DIR, exist_ok=True)
DB_PATH = os.path.join(CONFIG_DIR, "uchet.db")
TARIFF_PATH = os.path.join(CONFIG_DIR, "tariffs.json")  # формат v1.7: читается только при миграции

ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
RESET_ADMIN_PASSWORD = os.environ.get("RESET_ADMIN_PASSWORD", "0") == "1"
def _env_int(name, default):
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


AUDIT_KEEP_DAYS = max(0, _env_int("AUDIT_KEEP_DAYS", 365))          # сколько хранить журнал, 0 — всегда
SESSION_HOURS = _env_int("SESSION_HOURS", 12)                    # срок сессии без «Запомнить меня»
REMEMBER_DAYS = max(30, min(90, _env_int("REMEMBER_DAYS", 30)))  # срок с «Запомнить меня» (скользящий), 30–90 дней
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "0") == "1"
BASE_URL = os.environ.get("BASE_URL", "").strip().rstrip("/")  # внешний адрес для ссылок и QR ребёнка (необязательно)

if os.environ.get("TZ") and hasattr(time, "tzset"):
    time.tzset()


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def log(msg):
    print(f"[БД] {msg}", flush=True)


def audit_system(con, action, details=""):
    """Системная запись журнала (миграция, обновление версии, администратор, очистка): автор — «система»."""
    con.execute("INSERT INTO audit_log(ts,user_id,actor,child_id,action,details) VALUES(?,?,?,?,?,?)",
                (now_iso(), None, "система", None, action, str(details)[:300]))


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
        t = copy.deepcopy(DEFAULT_TARIFFS)
        for k in ("grade", "control"):
            t[k] = {int(g_): v for g_, v in t[k].items()}
        return t
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
# ───────────────────────────── СНИМКИ ДОГОВОРА (редакции) ─────────────────────────────
RULES_MAX = 4000
DEFAULT_RULES_TEXT = """Талантливый Ученик обязуется:

- не списывать и не хитрить;
- показывать дневник / электронный журнал без напоминаний;
- не тратить весь заработок на вредную еду (по согласованию с Инвестором).

Инвестор обязуется:

- не «пилить» за уроки, если тарифы выполнены;
- платить вовремя;
- не менять условия в одностороннем порядке.

Форс-мажор: болезнь, отключение света или интернета, внезапные гости с вкусным тортом — сроки сдачи домашки сдвигаются по согласию Сторон, штрафов нет."""


def _snapshot(tasks, grade, control, weekday, limit, c):
    grades = sorted(int(k) for k in grade)
    return {
        "v": 2,
        "tasks": [{"name": x["name"], "amount": int(x["amount"]), "once_per_day": bool(x["once_per_day"])} for x in tasks],
        "grade": {str(k): int(v) for k, v in grade.items()},
        "control": {str(k): int(v) for k, v in control.items()},
        "payout_weekday": int(weekday), "extra_payout_limit": int(limit),
        "grade_min": grades[0] if grades else 1, "grade_max": grades[-1] if grades else 10,
        "contract": {
            "number": str(c["number"]), "city": c["city"], "date": c["date"], "end": c["end"],
            "student_full": c["student_full"], "student_age": c["student_age"], "student_class": c["student_class"],
            "parents": [{"name": p["name"], "role": p["role"], "female": bool(p["female"])} for p in c["parents"]],
            "rules_text": c.get("rules_text") or None,
        },
    }


def snapshot_from_t(t, weekday):
    """Снимок договора из словаря tariffs (создание ребёнка)."""
    return _snapshot(t["tasks"], t["grade"], t["control"], weekday, t["extra_payout_limit"], t["contract"])


def snapshot_from_cfg(cfg):
    """Снимок договора по текущим настройкам ребёнка: тарифы, шкала, день и порог выплаты, данные и правила."""
    return _snapshot(cfg["tasks"], cfg["grade"], cfg["control"], cfg["payout_weekday"], cfg["extra_payout_limit"],
                     cfg["contract"])


def canon(snapshot):
    return json.dumps(snapshot, ensure_ascii=False, sort_keys=True)


def create_child(con, t, sort_order=0):
    """Создаёт ребёнка с тарифами, категориями и договором (+ первая редакция).
    Возвращает (child_id, {старый строковый id категории: новый числовой id})."""
    grades = sorted(int(k) for k in t["grade"])
    cid = con.execute(
        "INSERT INTO children(name,sort_order,payout_weekday,extra_payout_limit,grade_min,grade_max,created_at)"
        " VALUES(?,?,?,?,?,?,?)",
        (t["student"], sort_order, t.get("payout_weekday", DEFAULT_PAYOUT_WEEKDAY), t["extra_payout_limit"],
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
        "INSERT INTO contracts(child_id,number,city,date,end_date,student_full,student_age,student_class,rules_text)"
        " VALUES(?,?,?,?,?,?,?,?,?)",
        (cid, c["number"], c["city"], c["date"], c["end"], c["student_full"], c["student_age"],
         c["student_class"], c.get("rules_text") or None)).lastrowid
    for pos, p in enumerate(c["parents"]):
        con.execute("INSERT INTO contract_parties(contract_id,name,role,female,sort_order) VALUES(?,?,?,?,?)",
                    (ctr, p["name"], p["role"], int(p["female"]), pos))
    snapshot = snapshot_from_t(t, t.get("payout_weekday", DEFAULT_PAYOUT_WEEKDAY))
    con.execute("INSERT INTO contract_revisions(contract_id,effective_from,snapshot,created_at) VALUES(?,?,?,?)",
                (ctr, c["date"], canon(snapshot), now_iso()))
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
                audit_system(con, "system_migrate", (f"v1.7 → {VERSION}; резервная копия: {os.path.basename(bak)}"
                                                     if legacy else f"новая установка, версия {VERSION}"))
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
    if ADMIN_PASSWORD and len(ADMIN_PASSWORD) < 8:
        log("ВНИМАНИЕ: ADMIN_PASSWORD короче 8 символов — лучше задать пароль подлиннее (его можно сменить в «Мой аккаунт»).")
    con = sqlite3.connect(DB_PATH, timeout=10, isolation_level=None)
    try:
        row = con.execute("SELECT id,password_hash FROM users WHERE role='admin' ORDER BY id LIMIT 1").fetchone()
        if row is None:
            if ADMIN_PASSWORD:
                con.execute(
                    "INSERT INTO users(login,display_name,role,password_hash,must_change_password,can_payout,"
                    "all_children,is_active,created_at) VALUES('admin','Администратор','admin',?,0,1,1,1,?)",
                    (generate_password_hash(ADMIN_PASSWORD), now_iso()))
                audit_system(con, "system_admin_create", "логин admin, пароль из ADMIN_PASSWORD")
                log("создан администратор (логин admin) из ADMIN_PASSWORD")
            else:
                log("ВНИМАНИЕ: администратора нет и ADMIN_PASSWORD не задан — доступен только просмотр.")
        elif RESET_ADMIN_PASSWORD:
            if not ADMIN_PASSWORD:
                log("RESET_ADMIN_PASSWORD=1, но ADMIN_PASSWORD пуст — пароль не изменён.")
            elif not check_password_hash(row[1], ADMIN_PASSWORD):
                con.execute("UPDATE users SET password_hash=?, is_active=1, must_change_password=0 WHERE id=?",
                            (generate_password_hash(ADMIN_PASSWORD), row[0]))
                con.execute("DELETE FROM sessions WHERE subject_type='user' AND subject_id=?", (row[0],))
                con.execute("DELETE FROM login_attempts")
                audit_system(con, "system_admin_reset", "по RESET_ADMIN_PASSWORD=1, сессии отключены")
                log("пароль администратора СБРОШЕН из ADMIN_PASSWORD, сессии отключены. Уберите RESET_ADMIN_PASSWORD после входа.")
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
        return f.read().strip()  # подпись cookie с CSRF-токеном; вход хранится в БД (таблица sessions)


app = Flask(__name__)
app.secret_key = _secret_key()
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=COOKIE_SECURE,
    PERMANENT_SESSION_LIFETIME=timedelta(days=REMEMBER_DAYS),
    MAX_CONTENT_LENGTH=64 * 1024,
)
if os.environ.get("TRUST_PROXY", "0") == "1":
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)


# ───────────────────────────── БАЗА ─────────────────────────────
def get_db():
    if "db" not in g:
        con = sqlite3.connect(DB_PATH, timeout=10, isolation_level=None)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys=ON")
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
            "student_class": crow["student_class"], "rules_text": crow["rules_text"],
            "parents": parents or copy.deepcopy(DEFAULT_CONTRACT["parents"][:1]),
        }
    else:
        contract = copy.deepcopy(DEFAULT_CONTRACT)
    return {
        "id": ch["id"], "student": ch["name"], "archived": bool(ch["is_archived"]), "tasks": tasks,
        "extra_payout_limit": ch["extra_payout_limit"], "payout_weekday": ch["payout_weekday"],
        "grade": tariff["grade"], "control": tariff["control"],
        "grades": sorted(tariff["grade"], reverse=True),
        "grade_min": min(tariff["grade"]) if tariff["grade"] else 1,
        "grade_max": max(tariff["grade"]) if tariff["grade"] else 10,
        "contract": contract,
    }


def upgrade_revisions():
    """Один раз: автоматические редакции (созданные миграцией/добавлением ребёнка, created_by пуст) в старом формате
    обновляются до текущих настроек — иначе после обновления договор показал бы устаревшие суммы.
    Редакции, созданные кнопкой (created_by задан), и уже новые снимки не трогаются."""
    con = sqlite3.connect(DB_PATH, timeout=10, isolation_level=None)
    con.row_factory = sqlite3.Row
    try:
        # колонки шкалы у детей, созданных на свежей установке до этапа 5, содержали неверный максимум — выравниваем по тарифам
        con.execute("UPDATE children SET grade_min=(SELECT MIN(grade) FROM grade_tariffs WHERE child_id=children.id),"
                    " grade_max=(SELECT MAX(grade) FROM grade_tariffs WHERE child_id=children.id)"
                    " WHERE EXISTS (SELECT 1 FROM grade_tariffs WHERE child_id=children.id)")
        n = 0
        for r in con.execute("SELECT r.id, r.snapshot, c.child_id FROM contract_revisions r "
                             "JOIN contracts c ON c.id=r.contract_id WHERE r.created_by IS NULL").fetchall():
            try:
                snap = json.loads(r["snapshot"])
            except ValueError:
                snap = {}
            if snap.get("v", 1) >= 2:
                continue
            cfg = child_config(con, r["child_id"])
            if cfg is not None:
                con.execute("UPDATE contract_revisions SET snapshot=? WHERE id=?", (canon(snapshot_from_cfg(cfg)), r["id"]))
                n += 1
        if n:
            audit_system(con, "system_revisions", f"автоматических редакций договора обновлено: {n}")
            log(f"автоматические редакции договора обновлены до текущих настроек: {n}")
    except Exception as e:  # не фатально: старые снимки читаются с подстановкой текущих данных
        log(f"не удалось обновить автоматические редакции договора: {e}")
    finally:
        con.close()


upgrade_revisions()


def prune_audit(con):
    """Удаляет записи журнала старше AUDIT_KEEP_DAYS (0 — хранить всегда). Возвращает число удалённых."""
    if AUDIT_KEEP_DAYS <= 0:
        return 0
    cutoff = (datetime.now() - timedelta(days=AUDIT_KEEP_DAYS)).isoformat(timespec="seconds")
    n = con.execute("DELETE FROM audit_log WHERE ts < ?", (cutoff,)).rowcount
    if n:
        audit_system(con, "system_prune", f"удалено записей журнала старше {AUDIT_KEEP_DAYS} дн.: {n}")
    return n


def startup_housekeeping():
    """При запуске: запись о смене версии и очистка старого журнала."""
    con = sqlite3.connect(DB_PATH, timeout=10, isolation_level=None)
    try:
        row = (con.execute("SELECT value FROM meta WHERE key='app_version'").fetchone()
               or con.execute("SELECT value FROM meta WHERE key='created_by_version'").fetchone())
        if row and row[0] != VERSION:
            audit_system(con, "system_update", f"{row[0]} → {VERSION}")
        con.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('app_version',?)", (VERSION,))
        prune_audit(con)
    except Exception as e:  # журнал — вспомогательная функция, запуск из-за него не останавливаем
        log(f"не удалось выполнить служебные действия журнала: {e}")
    finally:
        con.close()


startup_housekeeping()


def current_child():
    """Ребёнок запроса: из адреса /c/<id>/… (доступ проверен в gate) или из сессии ребёнка на /me/…"""
    if "child" not in g:
        g.child = None
        cid = (request.view_args or {}).get("child_id") if request else None
        if request and request.endpoint in KID_ENDPOINTS:
            if g.get("kid"):
                g.child = child_config(get_db(), g.kid["child_id"])
        elif g.get("user") and cid is not None:
            g.child = child_config(get_db(), cid)
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

    all_grades = sorted(set(cfg["grades"]) | set(dist["grade"]) | set(dist["control"]), reverse=True)
    grade_blocks = []
    for kind, title in (("grade", "Оценки"), ("control", "Контрольные")):
        cnt = sum(dist[kind].values())
        avg = sum(g_ * c for g_, c in dist[kind].items()) / cnt if cnt else None
        mx = max(dist[kind].values(), default=0)
        grade_blocks.append({
            "title": title, "count": cnt, "avg": avg,
            "bars": [(g_, dist[kind].get(g_, 0), (dist[kind].get(g_, 0) / mx * 100) if mx else 0)
                     for g_ in all_grades],
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


# ───────────────────────────── АВТОРИЗАЦИЯ: серверные сессии и роли ─────────────────────────────
COOKIE_NAME = "sid"
ROLE_NAMES = {"admin": "Администратор", "adult": "Взрослый"}
MAX_FAILS, LOCK_SECONDS, ATTEMPT_WINDOW = 5, 300, 900  # 5 ошибок → пауза 5 минут; счётчик «остывает» за 15 минут
MIN_PASSWORD = 8
TOUCH_EVERY = 3600  # как часто (сек) обновлять last_seen и продлевать сессию
DUMMY_HASH = generate_password_hash(secrets.token_hex(8))  # одинаковое время ответа для несуществующих логинов
PUBLIC_ENDPOINTS = {"login", "healthz", "static", "manifest", "apple_icon", "kid_enter"}
KID_COOKIE = "ksid"
KID_ENDPOINTS = {"kid_home", "kid_history", "kid_stats", "kid_tariffs", "kid_contract", "kid_logout"}
PIN_RE = re.compile(r"[0-9]{4,8}")
PIN_MAX_FAILS, PIN_LOCK_SECONDS = 5, 300  # лимит PIN считается на ребёнка
BOT_RE = re.compile(r"bot|crawl|spider|preview|slurp|facebookexternalhit|whatsapp|telegram|skype|discord|vkshare|yandex", re.I)
MUST_CHANGE_ENDPOINTS = {"account", "account_password", "logout", "static", "healthz", "manifest", "apple_icon"}


def token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def describe_device(ua, ip):
    """Короткое описание устройства для списка «Мои устройства»."""
    os_name = next((n for k, n in (("iPhone", "iPhone"), ("iPad", "iPad"), ("Android", "Android"),
                                   ("Windows", "Windows"), ("Macintosh", "Mac"), ("CrOS", "ChromeOS"),
                                   ("Linux", "Linux")) if k in ua), "Устройство")
    browser = next((n for k, n in (("Edg/", "Edge"), ("EdgiOS", "Edge"), ("OPR/", "Opera"), ("YaBrowser", "Яндекс"),
                                   ("Firefox", "Firefox"), ("FxiOS", "Firefox"), ("CriOS", "Chrome"),
                                   ("Chrome", "Chrome"), ("Safari", "Safari")) if k in ua), "браузер")
    return f"{os_name} · {browser} ({ip})"[:120]


def audit(action, details="", child_id=None, who=None):
    """Запись в журнал действий. who=(user_id, login) — если действие не от текущего пользователя."""
    u = g.get("user")
    uid, actor = who if who else ((u["id"], u["login"]) if u else (None, None))
    get_db().execute("INSERT INTO audit_log(ts,user_id,actor,child_id,action,details) VALUES(?,?,?,?,?,?)",
                     (now_iso(), uid, actor, child_id, action, str(details)[:300]))


# — защита от перебора: счётчики в БД, отдельно на логин и на IP (и на смену пароля) —
def lock_state(con, scope, key):
    row = con.execute("SELECT locked_until FROM login_attempts WHERE scope=? AND key=?", (scope, key)).fetchone()
    return bool(row and row[0] > time.time())


def attempt_fail(con, scope, key):
    """Учитывает неудачную попытку. Возвращает True, если именно сейчас включилась блокировка."""
    now = time.time()
    row = con.execute("SELECT fails,locked_until,updated FROM login_attempts WHERE scope=? AND key=?",
                      (scope, key)).fetchone()
    locked_until = row["locked_until"] if row else 0
    fails = 1 if (not row or (now - row["updated"] > ATTEMPT_WINDOW and locked_until <= now)) else row["fails"] + 1
    newly = False
    if fails >= MAX_FAILS:
        fails, locked_until, newly = 0, now + LOCK_SECONDS, True
    con.execute("INSERT OR REPLACE INTO login_attempts(scope,key,fails,locked_until,updated) VALUES(?,?,?,?,?)",
                (scope, key, fails, locked_until, now))
    con.execute("DELETE FROM login_attempts WHERE updated < ? AND locked_until < ?", (now - 86400, now))
    return newly


def clear_attempts(con, scope, key):
    con.execute("DELETE FROM login_attempts WHERE scope=? AND key=?", (scope, key))


# — сессии: в cookie случайный токен, в БД только его хэш —
def create_session(con, user_id, remember):
    now = int(time.time())
    token = secrets.token_urlsafe(32)
    life = REMEMBER_DAYS * 86400 if remember else SESSION_HOURS * 3600
    device = describe_device(request.headers.get("User-Agent", ""), request.remote_addr or "?")
    con.execute("DELETE FROM sessions WHERE expires < ?", (now,))
    con.execute("INSERT INTO sessions(token_hash,subject_type,subject_id,created,last_seen,expires,remember,device)"
                " VALUES(?,?,?,?,?,?,?,?)", (token_hash(token), "user", user_id, now, now, now + life,
                                              int(remember), device))
    con.execute("DELETE FROM sessions WHERE subject_type='user' AND subject_id=? AND id NOT IN "
                "(SELECT id FROM sessions WHERE subject_type='user' AND subject_id=? ORDER BY created DESC, id DESC LIMIT 20)",
                (user_id, user_id))
    return token


def set_session_cookie(resp, token, max_age=None, name=None):
    resp.set_cookie(name or COOKIE_NAME, token, max_age=max_age, httponly=True, samesite="Lax",
                    secure=COOKIE_SECURE, path="/")


def load_session(token):
    """Пользователь по токену из cookie или None. Продлевает скользящий срок не чаще раза в TOUCH_EVERY."""
    con = get_db()
    now = int(time.time())
    row = con.execute(
        "SELECT s.id AS sid, s.last_seen, s.expires, s.remember, u.id AS id, u.login, u.display_name, u.role,"
        " u.can_payout, u.all_children, u.must_change_password, u.is_active"
        " FROM sessions s JOIN users u ON u.id=s.subject_id"
        " WHERE s.token_hash=? AND s.subject_type='user'", (token_hash(token),)).fetchone()
    if row is None:
        return None
    if row["expires"] <= now or not row["is_active"]:
        con.execute("DELETE FROM sessions WHERE id=?", (row["sid"],))
        return None
    if now - row["last_seen"] >= TOUCH_EVERY:
        life = REMEMBER_DAYS * 86400 if row["remember"] else SESSION_HOURS * 3600
        con.execute("UPDATE sessions SET last_seen=?, expires=? WHERE id=?", (now, now + life, row["sid"]))
        if row["remember"]:
            g.cookie_refresh = (token, life)
    return dict(row)


def is_admin():
    u = g.get("user")
    return bool(u) and u["role"] == "admin"


def can_pay():
    u = g.get("user")
    return bool(u) and (u["role"] == "admin" or bool(u["can_payout"]))


def admin_required(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        if not is_admin():
            flash("Недостаточно прав для этого действия.", "err")
            return redirect(url_for("home"))
        return fn(*a, **kw)
    return wrapper


def editor_required(fn):
    """Вошедший пользователь с доступом к ребёнку из адреса; для ребёнка в архиве запись закрыта."""
    @wraps(fn)
    def wrapper(*a, **kw):
        cc = current_child()
        if g.get("user") is None or cc is None:
            flash("Нет доступа.", "err")
            return redirect(url_for("home"))
        if cc["archived"]:
            flash("Ребёнок в архиве: записи, выплаты и правки закрыты.", "err")
            return redirect(url_for("index"))
        return fn(*a, **kw)
    return wrapper


@app.before_request
def gate():
    """Единая точка входа: сессии (взрослый и ребёнок) → CSRF → вход обязателен → смена пароля → доступ к ребёнку."""
    g.user = None
    g.kid = None
    ep = request.endpoint
    if ep is None or ep in ("static", "healthz", "apple_icon", "manifest"):  # 404 и служебные: без обращения к БД
        return
    tok = request.cookies.get(COOKIE_NAME)
    if tok:
        g.user = load_session(tok)
        if g.user is None:
            g.clear_cookie = True  # просроченный или отозванный токен
    ktok = request.cookies.get(KID_COOKIE)
    if ktok:
        g.kid = load_kid_session(ktok)
        if g.kid is None:
            g.clear_kid_cookie = True
    if request.method == "POST":
        sent = request.form.get("csrf", "").encode()
        csrf_tok = session.get("csrf", "").encode()
        if not csrf_tok or not hmac.compare_digest(sent, csrf_tok):
            flash("Сессия устарела, обновите страницу и повторите.", "err")
            return back()  # только на этот же хост
    if ep in PUBLIC_ENDPOINTS:
        return
    if ep in KID_ENDPOINTS:  # страницы ребёнка: нужна именно сессия ребёнка, сессия взрослого не подходит
        if g.kid is None:
            return render_template("kid_gone.html"), 401
        return
    if g.user is None:
        if g.kid is not None and ep == "home":
            return redirect(url_for("kid_home"))
        if request.method == "POST":
            flash("Сессия закончилась — войдите снова.", "err")
        return redirect(url_for("login"))
    if g.user["must_change_password"] and ep not in MUST_CHANGE_ENDPOINTS:
        return redirect(url_for("account"))
    cid = (request.view_args or {}).get("child_id")
    if cid is not None and cid not in {c["id"] for c in accessible_children()}:
        abort(404)  # чужой, архивный (для не-администратора) или несуществующий ребёнок
    g.url_child_id = cid if cid is not None else default_child_id()  # для url_for без явного child_id


@app.context_processor
def inject():
    if "csrf" not in session:
        session["csrf"] = secrets.token_hex(16)
        session.permanent = True
    cc = current_child()
    common = {"csrf": session["csrf"], "student": cc["student"] if cc else "", "role_names": ROLE_NAMES,
              "version": VERSION, "remember_days": REMEMBER_DAYS}
    if g.get("kid") is not None and request.endpoint in KID_ENDPOINTS:  # режим ребёнка: никаких прав на изменения
        return dict(common, kid_mode=True, user=None, uid=None, is_admin=False, can_edit=False, can_pay=False,
                    child_archived=False, nav_cid=None, switcher=[])
    u = g.get("user")
    active = [c for c in accessible_children() if not c["archived"]] if u else []
    ep = request.endpoint
    sw_ep = ep if (ep in ("index", "history", "stats", "contract") or (ep == "tariffs" and is_admin())) else "index"
    switcher = [{"id": c["id"], "name": c["name"], "url": url_for(sw_ep, child_id=c["id"]),
                 "on": cc is not None and cc["id"] == c["id"]} for c in active] if len(active) > 1 else []
    return dict(common, kid_mode=False, user=u, uid=u["id"] if u else None, is_admin=is_admin(),
                can_edit=bool(u) and cc is not None and not cc["archived"], can_pay=can_pay(),
                child_archived=bool(cc and cc["archived"]), nav_cid=g.get("url_child_id"), switcher=switcher)


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
    resp.headers["X-Robots-Tag"] = "noindex, nofollow"
    if request.endpoint == "kid_enter":
        resp.headers["Referrer-Policy"] = "no-referrer"  # токен в адресе не уходит наружу
    if g.get("cookie_refresh"):
        tok, life = g.cookie_refresh
        set_session_cookie(resp, tok, life)
    elif g.get("clear_cookie"):
        resp.delete_cookie(COOKIE_NAME, path="/")
    if g.get("kid_cookie_refresh"):
        tok, life = g.kid_cookie_refresh
        set_session_cookie(resp, tok, life, name=KID_COOKIE)
    elif g.get("clear_kid_cookie"):
        resp.delete_cookie(KID_COOKIE, path="/")
    return resp


@app.template_filter("dt")
def dt_filter(v):
    """Время из БД (эпоха или ISO-строка) → «дд.мм.гггг чч:мм»."""
    if v in (None, ""):
        return "—"
    d = datetime.fromtimestamp(v) if isinstance(v, (int, float)) else datetime.fromisoformat(v)
    return d.strftime("%d.%m.%Y %H:%M")


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


# ───────────────────────────── СТРАНИЦЫ РЕБЁНКА (/c/<id>/…) ─────────────────────────────
def overview_context(con, cfg):
    """Данные страницы «Обзор» — общие для взрослых (index.html) и ребёнка (me_index.html)."""
    cid = cfg["id"]
    bal = balance(con, cid)
    limit = cfg["extra_payout_limit"]
    recent = con.execute("SELECT * FROM events WHERE child_id=? ORDER BY date DESC, id DESC LIMIT 8", (cid,)).fetchall()
    return dict(
        bal=bal, limit=limit, progress=max(0, min(100, bal * 100 // limit)) if limit else 0,
        over=bal > limit, hint=pay_hint(cfg["payout_weekday"]), recent=recent,
        chart=chart_svg(weekly_series(con, cid, 8)),
        today=date.today().isoformat(), tasks=cfg["tasks"], grades=cfg["grades"],
        tariff_json=json.dumps({k: cfg[k] for k in ("grade", "control")}))


@app.route("/c/<int:child_id>/")
def index(child_id):
    return render_template("index.html", **overview_context(get_db(), current_child()))


@app.route("/c/<int:child_id>/history")
def history(child_id):
    con = get_db()
    cid = current_child()["id"]
    events = con.execute("SELECT * FROM events WHERE child_id=? ORDER BY date DESC, id DESC LIMIT 500",
                         (cid,)).fetchall()
    pays = con.execute("SELECT * FROM payouts WHERE child_id=? ORDER BY id DESC LIMIT 100", (cid,)).fetchall()
    return render_template("history.html", events=events, pays=pays)


@app.route("/c/<int:child_id>/stats")
def stats(child_id):
    con = get_db()
    cfg = current_child()
    return render_template("stats.html", s=compute_stats(con, cfg), chart=chart_svg(weekly_series(con, cfg["id"], 12)))


# ───────────────────────────── ДЕТИ: обзор, переключение, управление ─────────────────────────────
def accessible_children():
    """Дети, к которым у пользователя есть доступ, в порядке sort_order. Администратору видны и архивные."""
    if "acc_children" not in g:
        u = g.get("user")
        rows = []
        if u:
            con = get_db()
            if u["role"] == "admin":
                rows = con.execute("SELECT id,name,is_archived FROM children ORDER BY sort_order, id").fetchall()
            elif u["all_children"]:
                rows = con.execute("SELECT id,name,is_archived FROM children WHERE is_archived=0 "
                                   "ORDER BY sort_order, id").fetchall()
            else:
                rows = con.execute("SELECT c.id,c.name,c.is_archived FROM children c "
                                   "JOIN user_children uc ON uc.child_id=c.id WHERE uc.user_id=? AND c.is_archived=0 "
                                   "ORDER BY c.sort_order, c.id", (u["id"],)).fetchall()
        g.acc_children = [{"id": r["id"], "name": r["name"], "archived": bool(r["is_archived"])} for r in rows]
    return g.acc_children


def default_child_id():
    """Первый доступный неархивный ребёнок (администратору, если таких нет, — первый любой)."""
    acc = accessible_children()
    for c in acc:
        if not c["archived"]:
            return c["id"]
    return acc[0]["id"] if acc and is_admin() else None


@app.url_defaults
def add_child_id(endpoint, values):
    """url_for('history') внутри страницы ребёнка сам подставляет child_id текущего ребёнка."""
    if "child_id" in values:
        return
    cid = g.get("url_child_id")
    if cid and app.url_map.is_endpoint_expecting(endpoint, "child_id"):
        values["child_id"] = cid


@app.route("/", endpoint="home")
def home():
    active = [c for c in accessible_children() if not c["archived"]]
    if not active:
        if is_admin() and accessible_children():
            flash("Все дети в архиве — верните кого-нибудь из архива.", "err")
            return redirect(url_for("children"))
        return render_template("nochild.html")
    if len(active) == 1:  # один ребёнок — сразу его обзор
        return redirect(url_for("index", child_id=active[0]["id"]))
    con = get_db()
    today = date.today()
    mon = monday(today)
    cards = []
    for c in active:
        cfg = child_config(con, c["id"])
        bal = balance(con, c["id"])
        limit = cfg["extra_payout_limit"]
        week = con.execute(
            "SELECT COALESCE(SUM(amount),0) FROM events WHERE child_id=? AND type!='carry' AND date>=? AND date<=?",
            (c["id"], mon.isoformat(), (mon + timedelta(days=6)).isoformat())).fetchone()[0]
        cards.append({"id": c["id"], "name": c["name"], "bal": bal, "limit": limit, "week": week,
                      "progress": max(0, min(100, bal * 100 // limit)) if limit else 0,
                      "over": bal > limit, "hint": pay_hint(cfg["payout_weekday"]),
                      "pay_day": bal > 0 and today.weekday() == cfg["payout_weekday"]})
    return render_template("overview.html", cards=cards)


def _legacy_redirect(page):
    """Старые адреса (/history и т. д., закладки на домашнем экране) → тот же раздел первого ребёнка."""
    def view():
        cid = default_child_id()
        return redirect(url_for(page, child_id=cid) if cid else url_for("home"))
    view.__name__ = f"legacy_{page}"
    app.add_url_rule(f"/{page}", endpoint=f"legacy_{page}", view_func=view)


for _page in ("history", "stats", "tariffs", "contract"):
    _legacy_redirect(_page)


# — управление детьми (только администратор) —
def clean_child_name(raw):
    name = " ".join(raw.split())[:40]
    if not name:
        raise FormError("Укажите имя ребёнка.")
    return name


def child_name_taken(con, name, exclude_id=None):
    return any(r["name"].casefold() == name.casefold() and r["id"] != exclude_id
               for r in con.execute("SELECT id,name FROM children"))


def get_child_row_or_404(cid):
    row = get_db().execute("SELECT * FROM children WHERE id=?", (cid,)).fetchone()
    if row is None:
        abort(404)
    return row


@app.route("/children")
@admin_required
def children():
    con = get_db()
    items = [{"id": r["id"], "name": r["name"], "archived": bool(r["is_archived"]), "bal": balance(con, r["id"]),
              "access": kid_mode_of(r),
              "events": con.execute("SELECT COUNT(*) FROM events WHERE child_id=?", (r["id"],)).fetchone()[0]}
             for r in con.execute("SELECT id,name,is_archived,link_enabled,pin_enabled FROM children ORDER BY sort_order, id")]
    return render_template("children.html", items=items, year=date.today().year)


@app.route("/children/create", methods=["POST"], endpoint="child_create")
@admin_required
def child_create():
    con = get_db()
    f = request.form
    try:
        name = clean_child_name(f.get("name", ""))
        if child_name_taken(con, name):
            raise FormError("Ребёнок с таким именем уже есть.")
        src, raw = None, f.get("copy_from", "")
        if raw:
            src = child_config(con, int(raw)) if re.fullmatch(r"[0-9]{1,9}", raw) else None
            if src is None:
                raise FormError("Ребёнок для копирования не найден.")
    except FormError as e:
        flash(str(e), "err")
        return redirect(url_for("children"))
    first = con.execute("SELECT id FROM children ORDER BY sort_order, id LIMIT 1").fetchone()
    base = src or (child_config(con, first["id"]) if first else None)  # откуда брать подписантов, город, срок, шкалу
    scale = sorted(base["grade"]) if base else sorted(DEFAULT_TARIFFS["grade"], key=int)
    if src:
        tasks = [{"id": str(x["id"]), "name": x["name"], "amount": x["amount"], "once_per_day": x["once_per_day"]}
                 for x in src["tasks"]]
        grade, control, limit = dict(src["grade"]), dict(src["control"]), src["extra_payout_limit"]
    else:  # без копирования: категорий нет, суммы оценок 0 ₽ — настраиваются на странице «Тарифы»
        tasks, grade, control = [], {int(g_): 0 for g_ in scale}, {int(g_): 0 for g_ in scale}
        limit = DEFAULT_TARIFFS["extra_payout_limit"]
    bc = base["contract"] if base else DEFAULT_CONTRACT
    today = date.today()
    number = con.execute("SELECT COUNT(*) FROM contracts").fetchone()[0] + 1
    contract_data = {"number": f"{number}/{today.year}", "city": bc["city"], "date": today.isoformat(),
                     "end": bc["end"], "student_full": name, "student_age": 0, "student_class": 0,
                     "parents": [dict(p) for p in bc["parents"]]}
    tariffs_data = {"student": name, "tasks": tasks, "extra_payout_limit": limit, "grade": grade,
                    "control": control, "contract": contract_data,
                    "payout_weekday": src["payout_weekday"] if src else DEFAULT_PAYOUT_WEEKDAY}
    con.execute("BEGIN IMMEDIATE")
    try:
        order = con.execute("SELECT COALESCE(MAX(sort_order), -1) + 1 FROM children").fetchone()[0]
        new_id, _ = create_child(con, tariffs_data, order)
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    audit("child_create", f"{name}; копия из: {src['student'] if src else '—'}", child_id=new_id)
    flash(f"Ребёнок «{name}» добавлен. Тарифы — на странице «Тарифы», данные договора — «Договор → Править».", "ok")
    return redirect(url_for("children"))


@app.route("/children/<int:cid>/rename", methods=["POST"], endpoint="child_rename")
@admin_required
def child_rename(cid):
    con = get_db()
    ch = get_child_row_or_404(cid)
    try:
        name = clean_child_name(request.form.get("name", ""))
        if child_name_taken(con, name, exclude_id=cid):
            raise FormError("Ребёнок с таким именем уже есть.")
    except FormError as e:
        flash(str(e), "err")
        return redirect(url_for("children"))
    if name != ch["name"]:
        con.execute("UPDATE children SET name=? WHERE id=?", (name, cid))
        audit("child_rename", f"{ch['name']} → {name}", child_id=cid)
        flash("Имя изменено.", "ok")
    return redirect(url_for("children"))


@app.route("/children/<int:cid>/move", methods=["POST"], endpoint="child_move")
@admin_required
def child_move(cid):
    con = get_db()
    ch = get_child_row_or_404(cid)
    ids = [r["id"] for r in con.execute("SELECT id FROM children ORDER BY sort_order, id")]
    i = ids.index(cid)
    j = i - 1 if request.form.get("dir") == "up" else i + 1
    if 0 <= j < len(ids):
        ids[i], ids[j] = ids[j], ids[i]
        con.execute("BEGIN IMMEDIATE")
        try:
            for pos, x in enumerate(ids):
                con.execute("UPDATE children SET sort_order=? WHERE id=?", (pos, x))
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
        audit("child_move", f"{ch['name']}: позиция {i + 1} → {j + 1}", child_id=cid)
    return redirect(url_for("children"))


@app.route("/children/<int:cid>/archive", methods=["POST"], endpoint="child_archive")
@admin_required
def child_archive(cid):
    con = get_db()
    ch = get_child_row_or_404(cid)
    if ch["is_archived"]:
        return redirect(url_for("children"))
    if con.execute("SELECT COUNT(*) FROM children WHERE is_archived=0").fetchone()[0] <= 1:
        flash("Нельзя архивировать последнего активного ребёнка.", "err")
        return redirect(url_for("children"))
    con.execute("UPDATE children SET is_archived=1 WHERE id=?", (cid,))
    audit("child_archive", ch["name"], child_id=cid)
    flash(f"«{ch['name']}» в архиве: записи и выплаты закрыты, история и накопление сохранены.", "ok")
    return redirect(url_for("children"))


@app.route("/children/<int:cid>/restore", methods=["POST"], endpoint="child_restore")
@admin_required
def child_restore(cid):
    ch = get_child_row_or_404(cid)
    if ch["is_archived"]:
        get_db().execute("UPDATE children SET is_archived=0 WHERE id=?", (cid,))
        audit("child_restore", ch["name"], child_id=cid)
        flash(f"«{ch['name']}» возвращён из архива.", "ok")
    return redirect(url_for("children"))


# ───────────────────────────── ДОСТУП РЕБЁНКА: секретная ссылка и PIN ─────────────────────────────
def kid_mode_of(ch):
    return "pin" if ch["pin_enabled"] else "link" if ch["link_enabled"] else "off"


def child_link(token):
    """Полная ссылка ребёнка: BASE_URL (если задан) или адрес, по которому открыта страница «Доступ»."""
    base = BASE_URL or request.host_url.rstrip("/")
    return base + url_for("kid_enter", token=token)


def kid_who(ch):
    return (None, f"ребёнок {ch['name']}")


def create_kid_session(con, child_id, remember):
    now = int(time.time())
    token = secrets.token_urlsafe(32)
    life = REMEMBER_DAYS * 86400 if remember else SESSION_HOURS * 3600
    device = describe_device(request.headers.get("User-Agent", ""), request.remote_addr or "?")
    con.execute("DELETE FROM sessions WHERE expires < ?", (now,))
    con.execute("INSERT INTO sessions(token_hash,subject_type,subject_id,created,last_seen,expires,remember,device)"
                " VALUES(?,?,?,?,?,?,?,?)", (token_hash(token), "child", child_id, now, now, now + life,
                                              int(remember), device))
    con.execute("DELETE FROM sessions WHERE subject_type='child' AND subject_id=? AND id NOT IN "
                "(SELECT id FROM sessions WHERE subject_type='child' AND subject_id=? ORDER BY created DESC, id DESC LIMIT 20)",
                (child_id, child_id))
    return token, life, device


def load_kid_session(token):
    """Сессия ребёнка по токену из cookie или None. Закрытый доступ / архив / смена режима обнуляют сессии."""
    con = get_db()
    now = int(time.time())
    row = con.execute(
        "SELECT s.id AS sid, s.last_seen, s.expires, s.remember, c.id AS child_id, c.name, c.link_enabled, c.is_archived"
        " FROM sessions s JOIN children c ON c.id=s.subject_id"
        " WHERE s.token_hash=? AND s.subject_type='child'", (token_hash(token),)).fetchone()
    if row is None:
        return None
    if row["expires"] <= now or not row["link_enabled"] or row["is_archived"]:
        con.execute("DELETE FROM sessions WHERE id=?", (row["sid"],))
        return None
    if now - row["last_seen"] >= TOUCH_EVERY:
        life = REMEMBER_DAYS * 86400 if row["remember"] else SESSION_HOURS * 3600
        con.execute("UPDATE sessions SET last_seen=?, expires=? WHERE id=?", (now, now + life, row["sid"]))
        if row["remember"]:
            g.kid_cookie_refresh = (token, life)
    return dict(row)


def kid_login_response(con, ch, remember, how):
    token, life, device = create_kid_session(con, ch["id"], remember)
    audit("kid_login", f"{how}; {device}" + (", запомнить" if remember else ""), child_id=ch["id"], who=kid_who(ch))
    resp = redirect(url_for("kid_home"))  # токена в адресной строке больше нет
    set_session_cookie(resp, token, life if remember else None, name=KID_COOKIE)
    return resp


@app.route("/k/<token>", methods=["GET", "POST"], endpoint="kid_enter")
def kid_enter(token):
    con = get_db()
    ch = None
    if re.fullmatch(r"[A-Za-z0-9_-]{16,64}", token):
        ch = con.execute("SELECT * FROM children WHERE link_token=? AND link_enabled=1 AND is_archived=0",
                         (token,)).fetchone()
    if ch is None:
        abort(404)  # одинаково для неверной, отключённой и архивной ссылки
    if g.kid and g.kid["child_id"] == ch["id"]:
        return redirect(url_for("kid_home"))
    if not ch["pin_enabled"]:  # только ссылка: сессия «запомнить» со скользящим продлением
        if BOT_RE.search(request.headers.get("User-Agent", "")):
            return render_template("kid_gone.html", bot=True)  # предпросмотр ссылки в мессенджере не создаёт сессий
        return kid_login_response(con, ch, True, "ссылка")
    if request.method == "POST":
        now = time.time()
        pin = request.form.get("pin", "")[:16]
        if ch["pin_locked_until"] > now:
            flash(f"Слишком много попыток. Подождите {int((ch['pin_locked_until'] - now) // 60) + 1} мин.", "err")
        elif ch["pin_hash"] and check_password_hash(ch["pin_hash"], pin):
            con.execute("UPDATE children SET pin_fails=0 WHERE id=?", (ch["id"],))
            return kid_login_response(con, ch, request.form.get("remember") == "1", "ссылка + PIN")
        else:
            con.execute("UPDATE children SET pin_fails=pin_fails+1 WHERE id=?", (ch["id"],))
            fails = con.execute("SELECT pin_fails FROM children WHERE id=?", (ch["id"],)).fetchone()[0]
            audit("kid_pin_fail", request.remote_addr or "?", child_id=ch["id"], who=kid_who(ch))
            if fails >= PIN_MAX_FAILS:
                con.execute("UPDATE children SET pin_fails=0, pin_locked_until=? WHERE id=?",
                            (now + PIN_LOCK_SECONDS, ch["id"]))
                audit("kid_pin_locked", request.remote_addr or "?", child_id=ch["id"], who=kid_who(ch))
                flash("Неверный PIN. Слишком много попыток — подождите 5 минут.", "err")
            else:
                flash("Неверный PIN.", "err")
            time.sleep(0.7)
    return render_template("pin.html", name=ch["name"])


# — страницы ребёнка (только просмотр) —
@app.route("/me/", endpoint="kid_home")
def kid_home():
    return render_template("me_index.html", **overview_context(get_db(), current_child()))


@app.route("/me/history", endpoint="kid_history")
def kid_history():
    con = get_db()
    cid = current_child()["id"]
    events = con.execute("SELECT * FROM events WHERE child_id=? ORDER BY date DESC, id DESC LIMIT 500", (cid,)).fetchall()
    pays = con.execute("SELECT * FROM payouts WHERE child_id=? ORDER BY id DESC LIMIT 100", (cid,)).fetchall()
    return render_template("history.html", events=events, pays=pays)


@app.route("/me/stats", endpoint="kid_stats")
def kid_stats():
    con = get_db()
    cfg = current_child()
    return render_template("stats.html", s=compute_stats(con, cfg), chart=chart_svg(weekly_series(con, cfg["id"], 12)))


@app.route("/me/tariffs", endpoint="kid_tariffs")
def kid_tariffs():
    cfg = current_child()
    return render_template("tariffs.html", t=cfg, grades=cfg["grades"], weekdays=WEEKDAYS)


@app.route("/me/contract", endpoint="kid_contract")
def kid_contract():
    return contract_page(current_child(), kid=True)


@app.route("/me/logout", methods=["POST"], endpoint="kid_logout")
def kid_logout():
    audit("kid_logout", child_id=g.kid["child_id"], who=kid_who(g.kid))
    get_db().execute("DELETE FROM sessions WHERE id=?", (g.kid["sid"],))
    resp = redirect(url_for("kid_home"))
    resp.delete_cookie(KID_COOKIE, path="/")
    return resp


# — настройка доступа (только администратор) —
def kid_sessions(cid):
    return get_db().execute("SELECT * FROM sessions WHERE subject_type='child' AND subject_id=? "
                            "ORDER BY last_seen DESC, id DESC", (cid,)).fetchall()


def drop_kid_sessions(cid):
    get_db().execute("DELETE FROM sessions WHERE subject_type='child' AND subject_id=?", (cid,))


def qr_svg(link):
    return Markup(segno.make(link, error="m").svg_inline(scale=1, border=2, dark="#0a0e16", light="#ffffff",
                                                         omitsize=True))


@app.route("/children/<int:cid>/access", endpoint="child_access")
@admin_required
def child_access(cid):
    ch = get_child_row_or_404(cid)
    link = child_link(ch["link_token"]) if ch["link_enabled"] and ch["link_token"] else None
    return render_template("child_access.html", ch=ch, mode=kid_mode_of(ch), link=link,
                           qr=qr_svg(link) if link else None, has_pin=bool(ch["pin_hash"]),
                           locked_left=max(0, int(ch["pin_locked_until"] - time.time())),
                           sessions=kid_sessions(cid))


@app.route("/children/<int:cid>/access", methods=["POST"], endpoint="child_access_save")
@admin_required
def child_access_save(cid):
    ch = get_child_row_or_404(cid)
    f = request.form
    mode = f.get("mode", "")
    pin_raw, gen = f.get("pin", "").strip(), f.get("gen_pin") == "1"
    new_hash, shown = None, None
    if mode not in ("off", "link", "pin"):
        flash("Некорректный режим.", "err")
        return redirect(url_for("child_access", cid=cid))
    if mode == "pin":
        if gen:
            pin_raw = "".join(secrets.choice("0123456789") for _ in range(6))
            shown = pin_raw
        if pin_raw:
            if not PIN_RE.fullmatch(pin_raw):
                flash("PIN: от 4 до 8 цифр.", "err")
                return redirect(url_for("child_access", cid=cid))
            new_hash = generate_password_hash(pin_raw)
        elif not ch["pin_hash"]:
            flash("Задайте PIN (4–8 цифр) или отметьте «Сгенерировать PIN».", "err")
            return redirect(url_for("child_access", cid=cid))
    link_enabled, pin_enabled = int(mode != "off"), int(mode == "pin")
    token = ch["link_token"] or (secrets.token_urlsafe(24) if link_enabled else None)
    changed = link_enabled != ch["link_enabled"] or pin_enabled != ch["pin_enabled"] or new_hash is not None
    con = get_db()
    con.execute("UPDATE children SET link_enabled=?, pin_enabled=?, link_token=?, pin_hash=COALESCE(?, pin_hash),"
                " pin_fails=0, pin_locked_until=0 WHERE id=?", (link_enabled, pin_enabled, token, new_hash, cid))
    if changed:
        drop_kid_sessions(cid)  # смена режима или PIN отключает все устройства ребёнка
        audit("kid_access_update", f"режим: {mode}" + (", PIN изменён" if new_hash else ""), child_id=cid)
    flash("Сохранено." + (f" PIN: {shown} — показывается один раз, запишите." if shown else ""), "ok")
    return redirect(url_for("child_access", cid=cid))


@app.route("/children/<int:cid>/access/regenerate", methods=["POST"], endpoint="child_access_token")
@admin_required
def child_access_token(cid):
    get_child_row_or_404(cid)
    get_db().execute("UPDATE children SET link_token=? WHERE id=?", (secrets.token_urlsafe(24), cid))
    drop_kid_sessions(cid)
    audit("child_token_regen", "старая ссылка перестала работать", child_id=cid)
    flash("Новая ссылка создана. Старая и все устройства ребёнка отключены.", "ok")
    return redirect(url_for("child_access", cid=cid))


@app.route("/children/<int:cid>/access/unlock", methods=["POST"], endpoint="child_access_unlock")
@admin_required
def child_access_unlock(cid):
    get_child_row_or_404(cid)
    get_db().execute("UPDATE children SET pin_fails=0, pin_locked_until=0 WHERE id=?", (cid,))
    audit("kid_pin_unlock", "блокировка PIN снята", child_id=cid)
    flash("Блокировка PIN снята.", "ok")
    return redirect(url_for("child_access", cid=cid))


@app.route("/children/<int:cid>/access/sessions/<int:sid>/revoke", methods=["POST"], endpoint="child_access_revoke")
@admin_required
def child_access_revoke(cid, sid):
    get_child_row_or_404(cid)
    cur = get_db().execute("DELETE FROM sessions WHERE id=? AND subject_type='child' AND subject_id=?", (sid, cid))
    if cur.rowcount:
        audit("kid_session_revoke", f"сессия {sid}", child_id=cid)
        flash("Устройство отключено.", "ok")
    return redirect(url_for("child_access", cid=cid))


@app.route("/children/<int:cid>/access/logout-all", methods=["POST"], endpoint="child_access_logout_all")
@admin_required
def child_access_logout_all(cid):
    get_child_row_or_404(cid)
    drop_kid_sessions(cid)
    audit("kid_logout_all", "все устройства ребёнка", child_id=cid)
    flash("Все устройства ребёнка отключены.", "ok")
    return redirect(url_for("child_access", cid=cid))


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
    """Пример недели из реальных тарифов: «раз в день», обычное дело, высший оплачиваемый балл и контрольная."""
    parts = []
    once = next((x for x in t["tasks"] if x["once_per_day"] and x["amount"] > 0), None)
    chore = next((x for x in t["tasks"] if not x["once_per_day"] and x["amount"] > 0), None)
    for x in (once, chore):
        if x:
            parts.append((f"«{x['name']}»", x["amount"]))
    top_g = max((g_ for g_, v in t["grade"].items() if v > 0), default=None)
    if top_g is not None:
        parts.append((f"оценка «{top_g}»", t["grade"][top_g]))
    top_c = max((g_ for g_, v in t["control"].items() if v > 0), default=None)
    if top_c is not None:
        parts.append((f"«{top_c}» за контрольную", t["control"][top_c]))
    if len(parts) < 2:
        return None
    total = sum(a for _, a in parts)
    return ", ".join(f"{n} ({money(a, True)})" for n, a in parts) + f" — итого {money(total)}"


# ───────────────────────────── ДОГОВОР: редакции, шкала, правила игры ─────────────────────────────
WEEKDAYS_DAT = ["понедельникам", "вторникам", "средам", "четвергам", "пятницам", "субботам", "воскресеньям"]


def render_rules(text):
    """«Правила игры» → безопасный HTML. Пустая строка — новый абзац, строки «- …» — список. Всё экранируется."""
    out = []
    for block in re.split(r"\n\s*\n", (text or "").strip()):
        lines = [ln.strip() for ln in block.split("\n") if ln.strip()]
        if not lines:
            continue
        if all(ln.startswith("- ") for ln in lines):
            out.append("<ul>" + "".join(f"<li>{escape(ln[2:].strip())}</li>" for ln in lines) + "</ul>")
        else:
            out.append("<p>" + "<br>".join(str(escape(ln)) for ln in lines) + "</p>")
    return Markup("".join(out))


def view_from_cfg(cfg):
    """Договор по текущим настройкам (черновик)."""
    return {"tasks": cfg["tasks"], "grade": cfg["grade"], "control": cfg["control"],
            "payout_weekday": cfg["payout_weekday"], "extra_payout_limit": cfg["extra_payout_limit"],
            "contract": cfg["contract"]}


def view_from_snapshot(snap, cfg):
    """Договор по снимку редакции. Старые снимки (без данных договора) дополняются текущими."""
    ct = dict(snap.get("contract") or cfg["contract"])
    ct.setdefault("rules_text", None)
    return {
        "tasks": [{"name": x["name"], "amount": int(x["amount"]), "once_per_day": bool(x.get("once_per_day"))}
                  for x in snap["tasks"]],
        "grade": {int(k): int(v) for k, v in snap["grade"].items()},
        "control": {int(k): int(v) for k, v in snap["control"].items()},
        "payout_weekday": int(snap.get("payout_weekday", cfg["payout_weekday"])),
        "extra_payout_limit": int(snap.get("extra_payout_limit", cfg["extra_payout_limit"])),
        "contract": ct,
    }


def doc_context(view):
    grades = sorted(view["grade"])
    return {
        "c": view["contract"], "tasks": view["tasks"], "limit": view["extra_payout_limit"],
        "cols": grade_columns(view), "example": contract_example(view),
        "scale_lo": grades[0] if grades else "", "scale_hi": grades[-1] if grades else "",
        "payday_text": "по " + WEEKDAYS_DAT[view["payout_weekday"] % 7],
        "rules_html": render_rules(view["contract"].get("rules_text") or DEFAULT_RULES_TEXT),
    }


def contract_revisions(con, child_id):
    """Редакции договора ребёнка (по возрастанию id) со статусом: current / future / old."""
    crow = con.execute("SELECT id FROM contracts WHERE child_id=?", (child_id,)).fetchone()
    if crow is None:
        return []
    today = date.today().isoformat()
    revs = []
    for i, r in enumerate(con.execute("SELECT * FROM contract_revisions WHERE contract_id=? ORDER BY id", (crow["id"],))):
        try:
            snap = json.loads(r["snapshot"])
        except ValueError:
            snap = {}
        revs.append({"id": r["id"], "n": i + 1, "effective_from": r["effective_from"], "created_at": r["created_at"],
                     "created_by": r["created_by"], "snapshot": snap})
    if revs:
        past = [r for r in revs if r["effective_from"] <= today]
        eff = max(past, key=lambda r: (r["effective_from"], r["id"])) if past else \
            min(revs, key=lambda r: (r["effective_from"], r["id"]))
        for r in revs:
            r["status"] = "current" if r is eff else "future" if r["effective_from"] > today else "old"
    return revs


def contract_page(cfg, kid):
    """Страница договора: действующая редакция, любая редакция по ?rev=, администратору — черновик ?draft=1."""
    con = get_db()
    admin = is_admin() and not kid
    revs = contract_revisions(con, cfg["id"])
    eff = next((r for r in revs if r["status"] == "current"), None)
    draft = admin and request.args.get("draft") == "1"
    shown = None
    if not draft:
        raw = request.args.get("rev", "")
        if raw:
            shown = next((r for r in revs if str(r["id"]) == raw), None)
            if shown is None:
                abort(404)
        else:
            shown = eff
    view = view_from_snapshot(shown["snapshot"], cfg) if shown else view_from_cfg(cfg)
    drift = bool(admin and (not revs or revs[-1]["snapshot"] != snapshot_from_cfg(cfg)))
    return render_template("contract.html", **doc_context(view), rev=shown, revs=revs, draft=draft, drift=drift,
                           can_manage=admin and not cfg["archived"], today=date.today().isoformat())


@app.route("/c/<int:child_id>/contract")
def contract(child_id):
    return contract_page(current_child(), kid=False)


@app.route("/c/<int:child_id>/contract/edit", endpoint="contract_edit")
@admin_required
def contract_edit(child_id):
    c = current_child()["contract"]
    return render_template("contract_edit.html", c=c, rules=c.get("rules_text") or DEFAULT_RULES_TEXT, rules_max=RULES_MAX)


@app.route("/c/<int:child_id>/contract/edit", methods=["POST"], endpoint="contract_save")
@admin_required
@editor_required
def contract_save(child_id):
    f = request.form
    cid = current_child()["id"]
    try:
        ctr = parse_contract(f, form_int)
        rules = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", f.get("rules", "").replace("\r\n", "\n").replace("\r", "\n")).strip()
        if len(rules) > RULES_MAX:
            raise FormError(f"Правила игры: максимум {RULES_MAX} символов (сейчас {len(rules)}).")
    except FormError as e:
        flash(str(e), "err")
        return redirect(url_for("contract_edit"))
    con = get_db()
    crow = con.execute("SELECT id FROM contracts WHERE child_id=?", (cid,)).fetchone()
    con.execute("BEGIN IMMEDIATE")
    try:
        con.execute("UPDATE contracts SET number=?, city=?, date=?, end_date=?, student_full=?, student_age=?,"
                    " student_class=?, rules_text=? WHERE id=?",
                    (ctr["number"], ctr["city"], ctr["date"], ctr["end"], ctr["student_full"], ctr["student_age"],
                     ctr["student_class"], rules or None, crow["id"]))
        con.execute("DELETE FROM contract_parties WHERE contract_id=?", (crow["id"],))
        for pos, p in enumerate(ctr["parents"]):
            con.execute("INSERT INTO contract_parties(contract_id,name,role,female,sort_order) VALUES(?,?,?,?,?)",
                        (crow["id"], p["name"], p["role"], int(p["female"]), pos))
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    audit("contract_update", f"№ {ctr['number']}", child_id=cid)
    flash("Данные договора сохранены. В действующий договор они попадут после создания новой редакции.", "ok")
    return redirect(url_for("contract", draft=1))


@app.route("/c/<int:child_id>/contract/revision", methods=["POST"], endpoint="contract_revision_create")
@admin_required
@editor_required
def contract_revision_create(child_id):
    cfg = current_child()
    con = get_db()
    try:
        eff = parse_date(request.form.get("effective_from", ""))
    except ValueError:
        flash("Некорректная дата «действует с».", "err")
        return redirect(url_for("contract"))
    snap = snapshot_from_cfg(cfg)
    revs = contract_revisions(con, cfg["id"])
    if revs and revs[-1]["snapshot"] == snap:
        flash("С последней редакции ничего не изменилось — новая редакция не нужна.", "err")
        return redirect(url_for("contract"))
    crow = con.execute("SELECT id FROM contracts WHERE child_id=?", (cfg["id"],)).fetchone()
    con.execute("INSERT INTO contract_revisions(contract_id,effective_from,snapshot,created_at,created_by) VALUES(?,?,?,?,?)",
                (crow["id"], eff, canon(snap), now_iso(), g.user["id"]))
    audit("contract_revision", f"№ {len(revs) + 1}, действует с {eff}", child_id=cfg["id"])
    flash(f"Редакция № {len(revs) + 1} создана, действует с {datetime.strptime(eff, '%Y-%m-%d').strftime('%d.%m.%Y')}.", "ok")
    return redirect(url_for("contract"))


# ───────────────────────────── ТАРИФЫ ─────────────────────────────
@app.route("/c/<int:child_id>/tariffs")
@admin_required
def tariffs(child_id):
    cfg = current_child()
    return render_template("tariffs.html", t=cfg, grades=cfg["grades"], weekdays=WEEKDAYS)


def form_int(raw, lo, hi, what):
    try:
        v = int(str(raw).replace("−", "-").replace(" ", "").replace("\u00a0", ""))
    except ValueError:
        raise FormError(f"{what}: нужно целое число.")
    if not lo <= v <= hi:
        raise FormError(f"{what}: допустимо от {lo} до {hi}.")
    return v


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


@app.route("/c/<int:child_id>/tariffs", methods=["POST"], endpoint="tariffs_save")
@admin_required
@editor_required
def tariffs_save(child_id):
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
        payday = to_int(f.get("payday", str(cfg["payout_weekday"])), 0, 6, "День выплаты")
        old = cfg["grades"] or [1]
        gmin = to_int(f.get("gmin", str(min(old))), 0, 20, "Шкала оценок: от")
        gmax = to_int(f.get("gmax", str(max(old))), 0, 20, "Шкала оценок: до")
        if gmin >= gmax:
            raise FormError("Шкала оценок: «от» должно быть меньше «до».")
        scale = range(gmin, gmax + 1)  # новые баллы получают 0 ₽, убранные — удаляются из тарифов (история остаётся)
        grade = {g_: to_int(f.get(f"g{g_}", ""), -100000, 100000, f"Оценка {g_}") if g_ in cfg["grade"] else 0 for g_ in scale}
        control = {g_: to_int(f.get(f"c{g_}", ""), -100000, 100000, f"Контрольная {g_}") if g_ in cfg["control"] else 0
                   for g_ in scale}
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
        con.execute("UPDATE children SET name=?, extra_payout_limit=?, payout_weekday=?, grade_min=?, grade_max=? WHERE id=?",
                    (student, limit, payday, gmin, gmax, cid))
        con.execute("DELETE FROM grade_tariffs WHERE child_id=?", (cid,))
        for typ, vals in (("grade", grade), ("control", control)):
            for g_, v in vals.items():
                con.execute("INSERT INTO grade_tariffs(child_id,type,grade,amount) VALUES(?,?,?,?)", (cid, typ, g_, v))
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    renamed = sum(1 for x in ops if x["id"] and existing[x["id"]]["name"] != x["name"])
    audit("tariffs_save", f"лимит {limit} ₽, день выплаты: {WEEKDAYS[payday]}, шкала {gmin}–{gmax}, категорий {len(ops)}, "
                          f"удалено {len(deleted)}, переименовано {renamed}", child_id=cid)
    flash("Тарифы сохранены. Новые суммы действуют для будущих записей.", "ok")
    return redirect(url_for("tariffs"))


# ───────────────────────────── ВХОД / ВЫХОД ─────────────────────────────
@app.route("/login", methods=["GET", "POST"])
def login():
    if g.user:
        return redirect(url_for("home"))
    con = get_db()
    login_value = ""
    if request.method == "POST":
        ip = request.remote_addr or "?"
        login_value = request.form.get("login", "").strip()[:64]
        lkey = login_value.casefold()
        password = request.form.get("password", "")[:256]
        remember = request.form.get("remember") == "1"
        if lock_state(con, "ip", ip) or lock_state(con, "login", lkey):
            flash("Слишком много попыток. Подождите 5 минут.", "err")
        else:
            u = con.execute("SELECT * FROM users WHERE login=?", (login_value,)).fetchone() if login_value else None
            ok = check_password_hash(u["password_hash"] if u else DUMMY_HASH, password)
            if ok and u and u["is_active"]:
                clear_attempts(con, "login", lkey)
                token = create_session(con, u["id"], remember)
                con.execute("UPDATE users SET last_login=? WHERE id=?", (now_iso(), u["id"]))
                session.clear()  # новый CSRF-токен после входа
                session["csrf"] = secrets.token_hex(16)
                session.permanent = True
                device = describe_device(request.headers.get("User-Agent", ""), ip)
                audit("login", device + (", запомнить" if remember else ""), who=(u["id"], u["login"]))
                resp = redirect(url_for("home"))
                set_session_cookie(resp, token, REMEMBER_DAYS * 86400 if remember else None)
                return resp
            locked = attempt_fail(con, "ip", ip)
            locked = attempt_fail(con, "login", lkey) or locked
            if u:
                audit("login_fail", ip + ("" if u["is_active"] else " (аккаунт отключён)"), who=(u["id"], u["login"]))
            if locked:
                audit("login_locked", ip, who=(None, login_value or None))
            time.sleep(0.7)  # притормаживаем перебор
            flash("Неверный логин или пароль.", "err")
    no_users = con.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
    return render_template("login.html", login_value=login_value, no_users=no_users)


@app.route("/logout", methods=["POST"])
def logout():
    if g.user:
        get_db().execute("DELETE FROM sessions WHERE id=?", (g.user["sid"],))
        audit("logout")
    session.clear()
    resp = redirect(url_for("login"))
    resp.delete_cookie(COOKIE_NAME, path="/")
    return resp


# ───────────────────────────── МОЙ АККАУНТ ─────────────────────────────
class FormError(ValueError):
    """Ошибка ввода с понятным пользователю текстом."""


def validate_password(pw):
    if len(pw) < MIN_PASSWORD:
        raise FormError(f"Пароль: минимум {MIN_PASSWORD} символов.")
    if len(pw) > 128:
        raise FormError("Пароль: максимум 128 символов.")


def new_temp_password():
    alphabet = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKMNPQRSTUVWXYZ23456789"  # без похожих символов
    return "".join(secrets.choice(alphabet) for _ in range(10))


def user_sessions(user_id):
    return get_db().execute("SELECT * FROM sessions WHERE subject_type='user' AND subject_id=? "
                            "ORDER BY last_seen DESC, id DESC", (user_id,)).fetchall()


@app.route("/account")
def account():
    return render_template("account.html", sessions=user_sessions(g.user["id"]), current_sid=g.user["sid"],
                           must_change=bool(g.user["must_change_password"]))


@app.route("/account/password", methods=["POST"], endpoint="account_password")
def account_password():
    con = get_db()
    uid = g.user["id"]
    f = request.form
    if lock_state(con, "pw", str(uid)):
        flash("Слишком много попыток. Подождите 5 минут.", "err")
        return redirect(url_for("account"))
    row = con.execute("SELECT password_hash FROM users WHERE id=?", (uid,)).fetchone()
    current = f.get("current", "")[:256]
    if not check_password_hash(row["password_hash"], current):
        attempt_fail(con, "pw", str(uid))
        time.sleep(0.7)
        flash("Текущий пароль неверный.", "err")
        return redirect(url_for("account"))
    new = f.get("new", "")
    try:
        validate_password(new)
        if new != f.get("repeat", ""):
            raise FormError("Пароли не совпадают.")
        if new == current:
            raise FormError("Новый пароль должен отличаться от текущего.")
    except FormError as e:
        flash(str(e), "err")
        return redirect(url_for("account"))
    was_forced = bool(g.user["must_change_password"])
    con.execute("UPDATE users SET password_hash=?, must_change_password=0 WHERE id=?",
                (generate_password_hash(new), uid))
    con.execute("DELETE FROM sessions WHERE subject_type='user' AND subject_id=? AND id!=?", (uid, g.user["sid"]))
    clear_attempts(con, "pw", str(uid))
    audit("password_change", "остальные устройства отключены")
    flash("Пароль изменён. На остальных устройствах нужно войти заново.", "ok")
    return redirect(url_for("home") if was_forced else url_for("account"))


@app.route("/account/sessions/<int:sid>/revoke", methods=["POST"], endpoint="account_session_revoke")
def account_session_revoke(sid):
    if sid == g.user["sid"]:
        return logout()
    cur = get_db().execute("DELETE FROM sessions WHERE id=? AND subject_type='user' AND subject_id=?",
                           (sid, g.user["id"]))
    if cur.rowcount:
        audit("session_revoke", f"сессия {sid}")
        flash("Устройство отключено.", "ok")
    return redirect(url_for("account"))


@app.route("/account/logout-all", methods=["POST"], endpoint="account_logout_all")
def account_logout_all():
    get_db().execute("DELETE FROM sessions WHERE subject_type='user' AND subject_id=?", (g.user["id"],))
    audit("logout_all", "со своего аккаунта")
    session.clear()
    resp = redirect(url_for("login"))
    resp.delete_cookie(COOKIE_NAME, path="/")
    return resp


# ───────────────────────────── АККАУНТЫ (только администратор) ─────────────────────────────
LOGIN_RE = re.compile(r"[\w.-]{3,32}")


def valid_child_ids(raw):
    ids = {int(x) for x in raw if re.fullmatch(r"[0-9]{1,9}", x)}
    return sorted(ids & {r[0] for r in get_db().execute("SELECT id FROM children")})


def get_user_or_404(user_id):
    row = get_db().execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    if row is None:
        abort(404)
    return row


def children_list():
    return get_db().execute("SELECT id,name FROM children WHERE is_archived=0 ORDER BY sort_order, id").fetchall()


@app.route("/users")
@admin_required
def users():
    con = get_db()
    rows = con.execute(
        "SELECT u.*, (SELECT COUNT(*) FROM user_children WHERE user_id=u.id) AS n_children,"
        " (SELECT COUNT(*) FROM sessions WHERE subject_type='user' AND subject_id=u.id) AS n_sessions"
        " FROM users u ORDER BY u.is_active DESC, u.id").fetchall()
    logins = con.execute("SELECT * FROM audit_log WHERE action IN ('login','login_fail','login_locked','kid_login','kid_pin_fail','kid_pin_locked') "
                         "ORDER BY id DESC LIMIT 20").fetchall()
    return render_template("users.html", users=rows, children=children_list(), logins=logins)


@app.route("/users/create", methods=["POST"], endpoint="user_create")
@admin_required
def user_create():
    f = request.form
    try:
        login_name = f.get("login", "").strip()
        if not LOGIN_RE.fullmatch(login_name):
            raise FormError("Логин: 3–32 символа — буквы, цифры, точка, дефис, подчёркивание.")
        name = " ".join(f.get("display_name", "").split())[:40]
        if not name:
            raise FormError("Укажите имя.")
        role = f.get("role", "")
        if role not in ROLE_NAMES:
            raise FormError("Некорректная роль.")
        pw = f.get("password", "")
        generated = not pw
        if generated:
            pw = new_temp_password()
        else:
            validate_password(pw)
        child_ids = valid_child_ids(f.getlist("child"))
    except FormError as e:
        flash(str(e), "err")
        return redirect(url_for("users"))
    admin = role == "admin"
    can_payout = 1 if admin else int(f.get("can_payout") is not None)
    all_children = 1 if admin else int(f.get("all_children") is not None)
    con = get_db()
    con.execute("BEGIN IMMEDIATE")
    try:
        uid = con.execute(
            "INSERT INTO users(login,display_name,role,password_hash,must_change_password,can_payout,"
            "all_children,is_active,created_at) VALUES(?,?,?,?,1,?,?,1,?)",
            (login_name, name, role, generate_password_hash(pw), can_payout, all_children, now_iso())).lastrowid
        for cid in child_ids:
            con.execute("INSERT INTO user_children(user_id,child_id) VALUES(?,?)", (uid, cid))
        con.execute("COMMIT")
    except sqlite3.IntegrityError:
        con.execute("ROLLBACK")
        flash("Такой логин уже есть.", "err")
        return redirect(url_for("users"))
    except Exception:
        con.execute("ROLLBACK")
        raise
    audit("user_create", f"{login_name}, роль {role}, выплаты {can_payout}, все дети {all_children}, дети {child_ids}")
    flash(f"Аккаунт «{login_name}» создан."
          + (f" Временный пароль: {pw} — он показывается один раз." if generated else "")
          + " При первом входе пароль нужно сменить.", "ok")
    return redirect(url_for("users"))


@app.route("/users/<int:user_id>")
@admin_required
def user_edit(user_id):
    con = get_db()
    u = get_user_or_404(user_id)
    assigned = {r[0] for r in con.execute("SELECT child_id FROM user_children WHERE user_id=?", (user_id,))}
    events = con.execute("SELECT * FROM audit_log WHERE user_id=? ORDER BY id DESC LIMIT 15", (user_id,)).fetchall()
    return render_template("user_edit.html", u=u, assigned=assigned, children=children_list(),
                           sessions=user_sessions(user_id), events=events, me=user_id == g.user["id"])


@app.route("/users/<int:user_id>", methods=["POST"], endpoint="user_save")
@admin_required
def user_save(user_id):
    f = request.form
    u = get_user_or_404(user_id)
    me = user_id == g.user["id"]  # свою роль и активность менять нельзя — так не останется аккаунтов без администратора
    name = " ".join(f.get("display_name", "").split())[:40]
    role = u["role"] if me else f.get("role", u["role"])
    if not name or role not in ROLE_NAMES:
        flash("Проверьте имя и роль.", "err")
        return redirect(url_for("user_edit", user_id=user_id))
    active = 1 if me else int(f.get("is_active") is not None)
    admin = role == "admin"
    can_payout = 1 if admin else int(f.get("can_payout") is not None)
    all_children = 1 if admin else int(f.get("all_children") is not None)
    child_ids = valid_child_ids(f.getlist("child"))
    con = get_db()
    con.execute("BEGIN IMMEDIATE")
    try:
        con.execute("UPDATE users SET display_name=?, role=?, can_payout=?, all_children=?, is_active=? WHERE id=?",
                    (name, role, can_payout, all_children, active, user_id))
        con.execute("DELETE FROM user_children WHERE user_id=?", (user_id,))
        for cid in child_ids:
            con.execute("INSERT INTO user_children(user_id,child_id) VALUES(?,?)", (user_id, cid))
        if not active:
            con.execute("DELETE FROM sessions WHERE subject_type='user' AND subject_id=?", (user_id,))
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    audit("user_update", f"{u['login']}: роль {role}, выплаты {can_payout}, все дети {all_children}, дети {child_ids}")
    if active != u["is_active"]:
        audit("user_enable" if active else "user_disable", u["login"])
    flash("Сохранено.", "ok")
    return redirect(url_for("user_edit", user_id=user_id))


@app.route("/users/<int:user_id>/password", methods=["POST"], endpoint="user_password")
@admin_required
def user_password(user_id):
    u = get_user_or_404(user_id)
    if user_id == g.user["id"]:
        flash("Свой пароль меняется в разделе «Мой аккаунт».", "err")
        return redirect(url_for("user_edit", user_id=user_id))
    pw = request.form.get("password", "")
    generated = not pw
    try:
        if generated:
            pw = new_temp_password()
        else:
            validate_password(pw)
    except FormError as e:
        flash(str(e), "err")
        return redirect(url_for("user_edit", user_id=user_id))
    con = get_db()
    con.execute("UPDATE users SET password_hash=?, must_change_password=1 WHERE id=?",
                (generate_password_hash(pw), user_id))
    con.execute("DELETE FROM sessions WHERE subject_type='user' AND subject_id=?", (user_id,))
    clear_attempts(con, "login", u["login"].casefold())
    audit("password_reset", u["login"])
    flash("Пароль сброшен, все устройства отключены." + (f" Временный пароль: {pw} — показывается один раз." if generated else "")
          + " При входе пароль нужно сменить.", "ok")
    return redirect(url_for("user_edit", user_id=user_id))


@app.route("/users/<int:user_id>/sessions/<int:sid>/revoke", methods=["POST"], endpoint="user_session_revoke")
@admin_required
def user_session_revoke(user_id, sid):
    u = get_user_or_404(user_id)
    if user_id == g.user["id"]:
        return redirect(url_for("account"))
    cur = get_db().execute("DELETE FROM sessions WHERE id=? AND subject_type='user' AND subject_id=?", (sid, user_id))
    if cur.rowcount:
        audit("session_revoke", f"{u['login']}: сессия {sid}")
        flash("Устройство отключено.", "ok")
    return redirect(url_for("user_edit", user_id=user_id))


@app.route("/users/<int:user_id>/logout-all", methods=["POST"], endpoint="user_logout_all")
@admin_required
def user_logout_all(user_id):
    u = get_user_or_404(user_id)
    if user_id == g.user["id"]:
        return redirect(url_for("account"))
    get_db().execute("DELETE FROM sessions WHERE subject_type='user' AND subject_id=?", (user_id,))
    audit("logout_all", u["login"])
    flash("Пользователь вышел на всех устройствах.", "ok")
    return redirect(url_for("user_edit", user_id=user_id))


# ───────────────────────────── ИЗМЕНЕНИЯ (вошедшие пользователи) ─────────────────────────────
def parse_date(raw):
    d = datetime.strptime(raw, "%Y-%m-%d").date()
    if abs((d - date.today()).days) > 400:
        raise ValueError
    return d.isoformat()


def back():
    return redirect(request.referrer if request.referrer and request.referrer.startswith(request.host_url)
                    else url_for("home"))


@app.route("/c/<int:child_id>/add/quick", methods=["POST"])
@editor_required
def add_quick(child_id):
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
    add_event(con, cfg["id"], d, "task", task["name"], task["amount"], task_id=task["id"], author=g.user["id"])
    audit("event_add", f"{task['name']}: {task['amount']:+} ₽, дата {dmy(d)}", child_id=cfg["id"])
    flash(f"{task['name']}: {money(task['amount'], True)}", "ok")
    return back()


@app.route("/c/<int:child_id>/add/grade", methods=["POST"])
@editor_required
def add_grade(child_id):
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
    add_event(get_db(), cfg["id"], d, kind, descr, amount, grade=grade, author=g.user["id"])
    audit("event_add", f"{descr}: {amount:+} ₽, дата {dmy(d)}", child_id=cfg["id"])
    flash(f"{descr} → {money(amount, True)}", "ok")
    return back()


@app.route("/c/<int:child_id>/add/custom", methods=["POST"])
@editor_required
def add_custom(child_id):
    try:
        d = parse_date(request.form.get("date", ""))
        amount = int(request.form.get("amount", "").replace("−", "-").replace(" ", ""))
        if amount == 0 or abs(amount) > 100000:
            raise ValueError
    except ValueError:
        flash("Введите сумму целым числом (можно с минусом) и корректную дату.", "err")
        return back()
    note = request.form.get("note", "").strip()[:120] or "По договорённости"
    add_event(get_db(), current_child()["id"], d, "custom", note, amount, author=g.user["id"])
    audit("event_add", f"{note}: {amount:+} ₽, дата {dmy(d)}", child_id=current_child()["id"])
    flash(f"{note}: {money(amount, True)}", "ok")
    return back()


@app.route("/c/<int:child_id>/payout", methods=["POST"])
@editor_required
def payout(child_id):
    if not can_pay():
        flash("Выплаты отмечает администратор или взрослый, которому это разрешено.", "err")
        return redirect(url_for("index"))
    try:
        amount = int(request.form.get("amount", ""))
        expected = int(request.form.get("expected", ""))
        do_payout(get_db(), current_child()["id"], amount, expected, g.user["id"])
    except ValueError as e:
        flash(str(e) if str(e) and "invalid literal" not in str(e) else "Некорректная сумма.", "err")
    else:
        audit("payout", f"{amount} ₽ из накопленных {expected} ₽" + (f"; остаток {expected - amount} ₽ перенесён" if amount < expected else ""),
              child_id=current_child()["id"])
        flash(f"Выплата отмечена: {money(amount)}", "ok")
    return redirect(url_for("index"))


@app.route("/c/<int:child_id>/delete/<int:event_id>", methods=["POST"])
@editor_required
def delete_event(child_id, event_id):
    sql = "DELETE FROM events WHERE id=? AND child_id=? AND payout_id IS NULL AND type!='carry'"
    args = [event_id, current_child()["id"]]
    if not is_admin():  # взрослый удаляет только свои записи
        sql += " AND author=?"
        args.append(g.user["id"])
    con = get_db()
    ev = con.execute("SELECT e.descr, e.amount, e.date, u.login AS author_login FROM events e LEFT JOIN users u ON u.id=e.author"
                     " WHERE e.id=? AND e.child_id=?", (event_id, current_child()["id"])).fetchone()
    cur = con.execute(sql, args)
    if cur.rowcount and ev:
        audit("event_delete", f"{ev['descr']}: {ev['amount']:+} ₽, дата {dmy(ev['date'])}; автор записи: {ev['author_login'] or '—'}",
              child_id=current_child()["id"])
    flash("Запись удалена." if cur.rowcount else "Эту запись удалить нельзя (выплачена, служебная или добавлена не вами).",
          "ok" if cur.rowcount else "err")
    return back()


@app.route("/c/<int:child_id>/undo", methods=["POST"])
@editor_required
def undo(child_id):
    con = get_db()
    if is_admin():
        row = con.execute(
            "SELECT id,type FROM events WHERE child_id=? AND payout_id IS NULL ORDER BY id DESC LIMIT 1",
            (current_child()["id"],)).fetchone()
    else:  # взрослый отменяет свою последнюю невыплаченную запись
        row = con.execute(
            "SELECT id,type FROM events WHERE child_id=? AND payout_id IS NULL AND author=? AND type!='carry' "
            "ORDER BY id DESC LIMIT 1", (current_child()["id"], g.user["id"])).fetchone()
    if not row or row["type"] == "carry":
        flash("Отменять нечего.", "err")
    else:
        ev = con.execute("SELECT e.descr, e.amount, e.date, u.login AS author_login FROM events e "
                         "LEFT JOIN users u ON u.id=e.author WHERE e.id=?", (row["id"],)).fetchone()
        con.execute("DELETE FROM events WHERE id=?", (row["id"],))
        audit("event_undo", f"{ev['descr']}: {ev['amount']:+} ₽, дата {dmy(ev['date'])}; автор записи: {ev['author_login'] or '—'}",
              child_id=current_child()["id"])
        flash("Последняя запись отменена.", "ok")
    return back()


# ───────────────────────────── ЖУРНАЛ ДЕЙСТВИЙ (страница для администратора) ─────────────────────────────
AUDIT_LABELS = {
    "login": "вход", "login_fail": "неудачный вход", "login_locked": "блокировка входа", "logout": "выход",
    "logout_all": "выход на всех устройствах", "password_change": "смена пароля", "password_reset": "сброс пароля",
    "session_revoke": "отключено устройство", "user_create": "аккаунт создан", "user_update": "аккаунт изменён",
    "user_disable": "аккаунт отключён", "user_enable": "аккаунт включён",
    "child_create": "ребёнок добавлен", "child_rename": "ребёнок переименован", "child_move": "порядок детей",
    "child_archive": "ребёнок в архиве", "child_restore": "возврат из архива",
    "kid_access_update": "доступ ребёнка изменён", "child_token_regen": "новая ссылка ребёнка",
    "kid_pin_unlock": "блокировка PIN снята", "kid_session_revoke": "устройство ребёнка отключено",
    "kid_logout_all": "устройства ребёнка отключены", "kid_login": "вход ребёнка", "kid_logout": "выход ребёнка",
    "kid_pin_fail": "неверный PIN", "kid_pin_locked": "блокировка PIN",
    "event_add": "запись", "event_delete": "удаление записи", "event_undo": "отмена записи", "payout": "выплата",
    "tariffs_save": "тарифы сохранены", "contract_update": "договор изменён", "contract_revision": "новая редакция договора",
    "system_migrate": "создание/миграция базы", "system_update": "обновление версии",
    "system_admin_create": "создан администратор", "system_admin_reset": "сброс пароля администратора",
    "system_revisions": "обновление редакций договора", "system_prune": "очистка журнала",
}
AUDIT_FAIL = {"login_fail", "login_locked", "kid_pin_fail", "kid_pin_locked"}
AUDIT_PAGE = 50


@app.route("/audit", endpoint="audit_page")
@admin_required
def audit_page():
    con = get_db()
    prune_audit(con)
    a = request.args
    where, args, sel = [], [], {"child": "", "user": "", "action": "", "from": "", "to": ""}
    v = a.get("child", "")
    if re.fullmatch(r"[0-9]{1,9}", v):
        where.append("a.child_id=?")
        args.append(int(v))
        sel["child"] = v
    v = a.get("user", "")
    if v == "kid":
        where.append("a.actor LIKE 'ребёнок %'")
        sel["user"] = v
    elif v == "system":
        where.append("a.actor = 'система'")
        sel["user"] = v
    elif re.fullmatch(r"[0-9]{1,9}", v):
        where.append("a.user_id=?")
        args.append(int(v))
        sel["user"] = v
    v = a.get("action", "")
    if v in AUDIT_LABELS:
        where.append("a.action=?")
        args.append(v)
        sel["action"] = v
    for key, op, tail in (("from", ">=", "T00:00:00"), ("to", "<=", "T23:59:59")):
        try:
            d_ = datetime.strptime(a.get(key, ""), "%Y-%m-%d").date().isoformat()
        except ValueError:
            continue
        where.append(f"a.ts {op} ?")
        args.append(d_ + tail)
        sel[key] = d_
    cond = (" WHERE " + " AND ".join(where)) if where else ""
    total = con.execute("SELECT COUNT(*) FROM audit_log a" + cond, args).fetchone()[0]
    pages = max(1, -(-total // AUDIT_PAGE))
    try:
        page = min(max(1, int(a.get("page", "1"))), pages)
    except ValueError:
        page = 1
    rows = con.execute("SELECT a.*, c.name AS child_name FROM audit_log a LEFT JOIN children c ON c.id=a.child_id"
                       + cond + " ORDER BY a.id DESC LIMIT ? OFFSET ?", args + [AUDIT_PAGE, (page - 1) * AUDIT_PAGE]).fetchall()
    return render_template(
        "audit.html", rows=rows, total=total, page=page, pages=pages, sel=sel, labels=AUDIT_LABELS, fail=AUDIT_FAIL,
        filters={k: v for k, v in sel.items() if v},
        kids=con.execute("SELECT id,name FROM children ORDER BY sort_order, id").fetchall(),
        users=con.execute("SELECT id,login FROM users ORDER BY login").fetchall())


if __name__ == "__main__":
    from waitress import serve
    port = int(os.environ.get("PORT", "8080"))
    print(f"Мотиватор v{VERSION} запущен на порту {port}", flush=True)
    serve(app, host="0.0.0.0", port=port, threads=6)
