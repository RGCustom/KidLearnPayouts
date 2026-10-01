# -*- coding: utf-8 -*-
"""
Учёт по Договору № 1/2026 — веб-версия для Unraid.

Просмотр и статистика открыты всем, кто дошёл до порта.
Любые изменения (записи, выплаты, удаление) — только после ввода пароля ADMIN_PASSWORD.
"""
import hmac
import json
import os
import re
import secrets
import sqlite3
import threading
import time
from collections import defaultdict
from datetime import date, datetime, timedelta
from functools import wraps

from flask import (Flask, Response, flash, g, redirect, render_template, request,
                   send_from_directory, session, url_for)
from markupsafe import Markup
from werkzeug.middleware.proxy_fix import ProxyFix

VERSION = "1.6"
CONFIG_DIR = os.environ.get("CONFIG_DIR", "/config")
os.makedirs(CONFIG_DIR, exist_ok=True)
DB_PATH = os.path.join(CONFIG_DIR, "uchet.db")
TARIFF_PATH = os.path.join(CONFIG_DIR, "tariffs.json")

if os.environ.get("TZ") and hasattr(time, "tzset"):
    time.tzset()

# ───────────────────────────── ТАРИФЫ ─────────────────────────────
DEFAULT_TASKS = [
    {"id": "homework", "name": "Домашка до 18:00", "amount": 100, "once_per_day": True},
    {"id": "dishes", "name": "Посудомойка", "amount": 50, "once_per_day": False},
    {"id": "trash", "name": "Мусор", "amount": 50, "once_per_day": False},
]
DEFAULT_TARIFFS = {
    "student": "Максим",
    "tasks": DEFAULT_TASKS,
    "extra_payout_limit": 1000,
    "grade":   {"10": 200, "9": 200, "8": 100, "7": 0, "6": 0, "5": 0, "4": 0, "3": -100, "2": -100, "1": -100},
    "control": {"10": 1000, "9": 600, "8": 300, "7": 0, "6": 0, "5": 0, "4": 0, "3": -300, "2": -300, "1": -300},
}


def _write_tariffs(t):
    data = dict(t)
    data["grade"] = {str(k): v for k, v in t["grade"].items()}
    data["control"] = {str(k): v for k, v in t["control"].items()}
    tmp = TARIFF_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, TARIFF_PATH)


def load_tariffs():
    if not os.path.exists(TARIFF_PATH):
        with open(TARIFF_PATH, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_TARIFFS, f, ensure_ascii=False, indent=2)
    with open(TARIFF_PATH, encoding="utf-8") as f:
        t = json.load(f)
    merged = dict(DEFAULT_TARIFFS)
    merged.update(t)
    migrated = "tasks" not in t
    if migrated:  # старый формат: homework / dishes / trash отдельными ключами
        merged["tasks"] = [dict(x, amount=int(t.get(x["id"], x["amount"]))) for x in DEFAULT_TASKS]
        for k in ("homework", "dishes", "trash"):
            merged.pop(k, None)
    merged["tasks"] = [
        {"id": str(x["id"]), "name": str(x["name"])[:40], "amount": int(x["amount"]),
         "once_per_day": bool(x.get("once_per_day"))} for x in merged["tasks"]]
    for k in ("grade", "control"):
        merged[k] = {int(g_): int(v) for g_, v in merged[k].items()}
    if migrated:
        _write_tariffs(merged)
    return merged


KIND_NAMES = {
    "grade": "Оценки",
    "control": "Контрольные",
    "custom": "По договорённости",
    "carry": "Перенос остатка",
}

T, GRADES, TASKS, TASK_BY_ID = {}, [], [], {}
_tariff_lock = threading.Lock()


def apply_tariffs(t):
    """Подменяет тарифы «на лету» (без перезапуска)."""
    global T, GRADES, TASKS, TASK_BY_ID
    T = t
    GRADES = sorted(t["grade"], reverse=True)
    TASKS = t["tasks"]
    TASK_BY_ID = {x["id"]: x for x in TASKS}


def save_tariffs(t):
    with _tariff_lock:
        _write_tariffs(t)
        apply_tariffs(t)


apply_tariffs(load_tariffs())

# ───────────────────────────── ПРИЛОЖЕНИЕ ─────────────────────────────
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
SESSION_HOURS = int(os.environ.get("SESSION_HOURS", "12"))


def _secret_key():
    path = os.path.join(CONFIG_DIR, "secret.key")
    if not os.path.exists(path):
        with open(path, "w") as f:
            f.write(secrets.token_hex(32))
        os.chmod(path, 0o600)
    with open(path) as f:
        base = f.read().strip()
    # смена пароля автоматически разлогинивает все сессии
    return hmac.new(base.encode(), ADMIN_PASSWORD.encode(), "sha256").hexdigest()


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


def init_db():
    con = sqlite3.connect(DB_PATH, isolation_level=None)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("""CREATE TABLE IF NOT EXISTS payouts(
        id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT NOT NULL, amount INTEGER NOT NULL)""")
    con.execute("""CREATE TABLE IF NOT EXISTS events(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT NOT NULL, kind TEXT NOT NULL, descr TEXT NOT NULL,
        grade INTEGER, amount INTEGER NOT NULL,
        payout_id INTEGER REFERENCES payouts(id))""")
    con.close()


init_db()


def balance(con):
    return con.execute("SELECT COALESCE(SUM(amount),0) FROM events WHERE payout_id IS NULL").fetchone()[0]


def add_event(con, d, kind, descr, amount, grade=None):
    con.execute("INSERT INTO events(date,kind,descr,grade,amount) VALUES(?,?,?,?,?)",
                (d, kind, descr, grade, amount))


def do_payout(con, amount, expected):
    con.execute("BEGIN IMMEDIATE")
    try:
        bal = balance(con)
        if bal != expected:
            raise ValueError("Сумма в накоплении изменилась — проверьте и повторите.")
        if not 1 <= amount <= bal:
            raise ValueError("Сумма выплаты должна быть от 1 ₽ до накопленной.")
        today = date.today().isoformat()
        pid = con.execute("INSERT INTO payouts(date,amount) VALUES(?,?)", (today, amount)).lastrowid
        con.execute("UPDATE events SET payout_id=? WHERE payout_id IS NULL", (pid,))
        if amount < bal:
            add_event(con, today, "carry", "Остаток после частичной выплаты", bal - amount)
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise


# ───────────────────────────── СТАТИСТИКА ─────────────────────────────
def monday(d):
    return d - timedelta(days=d.weekday())


def weekly_series(con, weeks):
    rows = con.execute("SELECT date, amount FROM events WHERE kind!='carry'").fetchall()
    acc = defaultdict(int)
    for r in rows:
        acc[monday(datetime.strptime(r["date"], "%Y-%m-%d").date())] += r["amount"]
    start = monday(date.today())
    return [(start - timedelta(weeks=i), acc.get(start - timedelta(weeks=i), 0)) for i in range(weeks - 1, -1, -1)]


def compute_stats(con):
    rows = con.execute("SELECT date,kind,grade,amount,descr FROM events WHERE kind!='carry' ORDER BY date, id").fetchall()
    earned = sum(r["amount"] for r in rows if r["amount"] > 0)
    fines = sum(r["amount"] for r in rows if r["amount"] < 0)
    pays = con.execute("SELECT id,date,amount FROM payouts ORDER BY id DESC").fetchall()
    paid = sum(p["amount"] for p in pays)

    per = {}  # kind -> [кол-во, сумма, последнее название]
    dist = {"grade": defaultdict(int), "control": defaultdict(int)}
    days = set()
    for r in rows:
        p = per.setdefault(r["kind"], [0, 0, ""])
        p[0] += 1
        p[1] += r["amount"]
        p[2] = r["descr"]
        if r["kind"] in dist and r["grade"] is not None:
            dist[r["kind"]][r["grade"]] += 1
        days.add(r["date"])

    grade_blocks = []
    for kind, title in (("grade", "Оценки"), ("control", "Контрольные")):
        cnt = sum(dist[kind].values())
        avg = sum(g_ * c for g_, c in dist[kind].items()) / cnt if cnt else None
        mx = max(dist[kind].values(), default=0)
        grade_blocks.append({
            "title": title, "count": cnt, "avg": avg,
            "bars": [(g_, dist[kind].get(g_, 0), (dist[kind].get(g_, 0) / mx * 100) if mx else 0) for g_ in GRADES],
        })
    special = ("grade", "control", "custom")
    order = ([x["id"] for x in TASKS if x["id"] in per]
             + [k for k in per if k not in TASK_BY_ID and k not in special]
             + [k for k in special if k in per])

    def label(k):
        if k in TASK_BY_ID:
            return TASK_BY_ID[k]["name"]
        return KIND_NAMES[k] if k in special else f"{per[k][2]} (удалена)"

    net = earned + fines
    return {
        "earned": earned, "fines": fines, "net": net, "paid": paid,
        "balance": balance(con), "pays": pays, "pay_count": len(pays),
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


def pay_hint():
    today = date.today()
    days = (4 - today.weekday()) % 7
    if days == 0:
        return "Сегодня пятница — день выплаты"
    return f"Ближайшая выплата — пятница, {(today + timedelta(days=days)).strftime('%d.%m.%Y')}"


# ───────────────────────────── АВТОРИЗАЦИЯ ─────────────────────────────
FAILS = {}  # ip -> [count, locked_until]


def client_locked(ip):
    rec = FAILS.get(ip)
    return bool(rec and rec[1] > time.time())


def register_fail(ip):
    rec = FAILS.setdefault(ip, [0, 0])
    rec[0] += 1
    if rec[0] >= 5:
        rec[0], rec[1] = 0, time.time() + 300
    time.sleep(0.7)  # притормаживаем перебор


def is_admin():
    return bool(ADMIN_PASSWORD) and session.get("admin") is True


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
        "csrf": session["csrf"], "is_admin": is_admin(), "student": T["student"],
        "read_only_mode": not ADMIN_PASSWORD, "version": VERSION,
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


# ───────────────────────────── СТРАНИЦЫ (открытые) ─────────────────────────────
@app.route("/")
def index():
    con = get_db()
    bal = balance(con)
    limit = T["extra_payout_limit"]
    recent = con.execute("SELECT * FROM events ORDER BY date DESC, id DESC LIMIT 8").fetchall()
    return render_template(
        "index.html", bal=bal, limit=limit, progress=max(0, min(100, bal * 100 // limit)) if limit else 0,
        over=bal > limit, hint=pay_hint(), recent=recent, chart=chart_svg(weekly_series(con, 8)),
        today=date.today().isoformat(), tasks=TASKS, grades=GRADES,
        tariff_json=json.dumps({k: T[k] for k in ("grade", "control")}))


@app.route("/history")
def history():
    con = get_db()
    events = con.execute("SELECT * FROM events ORDER BY date DESC, id DESC LIMIT 500").fetchall()
    pays = con.execute("SELECT * FROM payouts ORDER BY id DESC LIMIT 100").fetchall()
    return render_template("history.html", events=events, pays=pays)


@app.route("/stats")
def stats():
    con = get_db()
    return render_template("stats.html", s=compute_stats(con), chart=chart_svg(weekly_series(con, 12)))


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


# ───────────────────────────── ТАРИФЫ ─────────────────────────────
@app.route("/tariffs")
def tariffs():
    return render_template("tariffs.html", t=T, grades=GRADES)


class FormError(ValueError):
    """Ошибка ввода с понятным пользователю текстом."""


@app.route("/tariffs", methods=["POST"], endpoint="tariffs_save")
@admin_required
def tariffs_save():
    f = request.form

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
        tasks, names, ids = [], set(), set()
        for idx in dict.fromkeys(f.getlist("task_idx")):
            if not re.fullmatch(r"\w{1,24}", idx):
                raise FormError("Форма категорий повреждена — обновите страницу.")
            tid = f.get(f"task_{idx}_id", "").strip()
            name = f.get(f"task_{idx}_name", "").strip()[:40]
            raw = f.get(f"task_{idx}_amount", "").strip()
            if tid and f.get(f"task_{idx}_del") is not None:
                continue  # категория удалена (старые записи остаются в истории)
            if not tid and not name and not raw:
                continue  # пустая строка «добавить»
            if tid and tid not in TASK_BY_ID:
                raise FormError("Категория не найдена — обновите страницу.")
            if not name:
                raise FormError("У категории должно быть название.")
            if name.casefold() in names:
                raise FormError(f"Категория «{name}» указана дважды.")
            names.add(name.casefold())
            amount = to_int(raw, -100000, 100000, f"«{name}»")
            if not tid:
                while True:
                    tid = "t" + secrets.token_hex(3)
                    if tid not in TASK_BY_ID and tid not in ids:
                        break
            ids.add(tid)
            tasks.append({"id": tid, "name": name, "amount": amount, "once_per_day": f.get(f"task_{idx}_once") is not None})
        if len(tasks) > 30:
            raise FormError("Слишком много категорий (максимум 30).")
        return tasks

    try:
        student = f.get("student", "").strip()[:40]
        if not student:
            raise FormError("Укажите имя ученика.")
        new = {
            "student": student,
            "tasks": parse_tasks(),
            "extra_payout_limit": to_int(f.get("limit", ""), 0, 100000, "Выплата вне очереди"),
            "grade": {g: to_int(f.get(f"g{g}", ""), -100000, 100000, f"Оценка {g}") for g in GRADES},
            "control": {g: to_int(f.get(f"c{g}", ""), -100000, 100000, f"Контрольная {g}") for g in GRADES},
        }
    except FormError as e:
        flash(str(e), "err")
        return redirect(url_for("tariffs"))
    con = get_db()
    for x in new["tasks"]:  # переименование категории обновляет и старые записи
        old = TASK_BY_ID.get(x["id"])
        if old and old["name"] != x["name"]:
            con.execute("UPDATE events SET descr=? WHERE kind=?", (x["name"], x["id"]))
    save_tariffs(new)
    flash("Тарифы сохранены. Новые суммы действуют для будущих записей.", "ok")
    return redirect(url_for("tariffs"))


# ───────────────────────────── ВХОД / ВЫХОД ─────────────────────────────
@app.route("/login", methods=["GET", "POST"])
def login():
    if is_admin():
        return redirect(url_for("index"))
    if request.method == "POST":
        ip = request.remote_addr or "?"
        if not ADMIN_PASSWORD:
            flash("Пароль не задан в настройках контейнера (ADMIN_PASSWORD).", "err")
        elif client_locked(ip):
            flash("Слишком много попыток. Подождите 5 минут.", "err")
        elif hmac.compare_digest(request.form.get("password", "").encode(), ADMIN_PASSWORD.encode()):
            FAILS.pop(ip, None)
            session.clear()
            session["admin"] = True
            session["csrf"] = secrets.token_hex(16)
            session.permanent = True
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
    kind = request.form.get("kind", "")
    try:
        d = parse_date(request.form.get("date", ""))
    except ValueError:
        flash("Некорректная дата.", "err")
        return back()
    task = TASK_BY_ID.get(kind)
    if not task:
        flash("Такой категории нет (возможно, её удалили) — обновите страницу.", "err")
        return back()
    con = get_db()
    if task["once_per_day"] and con.execute(
            "SELECT 1 FROM events WHERE date=? AND kind=?", (d, kind)).fetchone():
        flash(f"«{task['name']}»: за эту дату уже записано.", "err")
        return back()
    add_event(con, d, kind, task["name"], task["amount"])
    flash(f"{task['name']}: {money(task['amount'], True)}", "ok")
    return back()


@app.route("/add/grade", methods=["POST"])
@admin_required
def add_grade():
    kind = request.form.get("kind", "")
    try:
        d = parse_date(request.form.get("date", ""))
        grade = int(request.form.get("grade", ""))
        amount = T[kind][grade]
    except (ValueError, KeyError):
        flash("Некорректные данные оценки.", "err")
        return back()
    descr = f"{'Контрольная' if kind == 'control' else 'Оценка'}: {grade}"
    add_event(get_db(), d, kind, descr, amount, grade)
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
    add_event(get_db(), d, "custom", note, amount)
    flash(f"{note}: {money(amount, True)}", "ok")
    return back()


@app.route("/payout", methods=["POST"])
@admin_required
def payout():
    try:
        amount = int(request.form.get("amount", ""))
        expected = int(request.form.get("expected", ""))
        do_payout(get_db(), amount, expected)
    except ValueError as e:
        flash(str(e) if str(e) and "invalid literal" not in str(e) else "Некорректная сумма.", "err")
    else:
        flash(f"Выплата отмечена: {money(amount)}", "ok")
    return redirect(url_for("index"))


@app.route("/delete/<int:event_id>", methods=["POST"])
@admin_required
def delete_event(event_id):
    cur = get_db().execute(
        "DELETE FROM events WHERE id=? AND payout_id IS NULL AND kind!='carry'", (event_id,))
    flash("Запись удалена." if cur.rowcount else "Эту запись удалить нельзя (уже выплачена или служебная).",
          "ok" if cur.rowcount else "err")
    return back()


@app.route("/undo", methods=["POST"])
@admin_required
def undo():
    con = get_db()
    row = con.execute(
        "SELECT id,kind FROM events WHERE payout_id IS NULL ORDER BY id DESC LIMIT 1").fetchone()
    if not row or row["kind"] == "carry":
        flash("Отменять нечего.", "err")
    else:
        con.execute("DELETE FROM events WHERE id=?", (row["id"],))
        flash("Последняя запись отменена.", "ok")
    return back()


if __name__ == "__main__":
    from waitress import serve
    port = int(os.environ.get("PORT", "8080"))
    if not ADMIN_PASSWORD:
        print("ВНИМАНИЕ: ADMIN_PASSWORD не задан — приложение работает только на просмотр.", flush=True)
    print(f"Учёт запущен на порту {port}", flush=True)
    serve(app, host="0.0.0.0", port=port, threads=6)
