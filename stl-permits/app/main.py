"""
STL Permits — daily digest + public archive of City of St. Louis building permits.

Companion to stl-meetings. Clones the same architecture:
- Flask + gunicorn + SQLite
- Cloudflare Worker proxy (stl-proxy) for stlouis-mo.gov fetches (VPS datacenter IPs blocked by city)
- Hostinger SMTP for outgoing mail
- Nginx Proxy Manager in front for TLS
- Background scheduler thread for periodic syncs and daily digest
"""

import csv
import hashlib
import io
import logging
import os
import secrets
import smtplib
import sqlite3
import threading
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Iterable

import requests
import schedule
from flask import Flask, abort, g, jsonify, redirect, render_template, request, url_for


# -----------------------------------------------------------------------------
# Config
# -----------------------------------------------------------------------------
DB_PATH = "/app/data/permits.db"

SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.hostinger.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "465"))
SMTP_USER = os.environ.get("SMTP_USER", "permits@veiledprofits.com")
SMTP_PASS = os.environ.get("SMTP_PASS", "")
BASE_URL = os.environ.get("BASE_URL", "https://permits.veiledprofits.com")

PROXY_BASE = os.environ.get(
    "PROXY_BASE", "https://stl-proxy.dan-f8a.workers.dev/?url="
)
PERMITS_CSV_URL = (
    "https://www.stlouis-mo.gov/customcf/endpoints/building-permits/"
    "building-permits-30-days-export.cfm?permitType=all&dataType=csv"
)

DIGEST_HOUR = int(os.environ.get("DIGEST_HOUR", "7"))
SYNC_INTERVAL_HOURS = int(os.environ.get("SYNC_INTERVAL_HOURS", "6"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("stl-permits")

app = Flask(__name__)


# -----------------------------------------------------------------------------
# Database
# -----------------------------------------------------------------------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS permits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dedup_key TEXT UNIQUE NOT NULL,
    address TEXT,
    application_date DATE,
    application_description TEXT,
    days_to_issue INTEGER,
    est_project_cost REAL,
    issue_date DATE,
    project_type TEXT,
    structure_type TEXT,
    first_seen DATETIME DEFAULT CURRENT_TIMESTAMP,
    notified INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_permits_issue_date ON permits(issue_date DESC);
CREATE INDEX IF NOT EXISTS idx_permits_first_seen ON permits(first_seen DESC);
CREATE INDEX IF NOT EXISTS idx_permits_address ON permits(address);
CREATE INDEX IF NOT EXISTS idx_permits_project_type ON permits(project_type);

CREATE TABLE IF NOT EXISTS subscribers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT UNIQUE NOT NULL,
    verified INTEGER DEFAULT 0,
    verify_token TEXT NOT NULL,
    unsubscribe_token TEXT NOT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    verified_at DATETIME
);

CREATE TABLE IF NOT EXISTS sync_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ran_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    total_rows INTEGER,
    new_permits INTEGER,
    error TEXT
);

CREATE TABLE IF NOT EXISTS digest_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sent_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    permit_count INTEGER,
    subscriber_count INTEGER
);
"""


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA journal_mode=WAL")
    return g.db


def get_db_standalone():
    """For use outside of request context (background thread)."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    conn = get_db_standalone()
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()
    log.info("DB initialized at %s", DB_PATH)


# -----------------------------------------------------------------------------
# Data fetch & parse
# -----------------------------------------------------------------------------
def dedup_key(address: str, app_date: str, desc: str, cost: str) -> str:
    payload = f"{address}|{app_date}|{desc}|{cost}".encode("utf-8", errors="replace")
    return hashlib.sha1(payload).hexdigest()[:16]


def fetch_permits_csv() -> str:
    """Pull the 30-day CSV via the Cloudflare Worker proxy (Hostinger is IP-blocked direct)."""
    proxied = PROXY_BASE + urllib.parse.quote(PERMITS_CSV_URL, safe="")
    log.info("Fetching permits CSV via proxy")
    r = requests.get(proxied, timeout=60)
    r.raise_for_status()
    text = r.text
    log.info("Got %d bytes", len(text))
    # City's ColdFusion endpoint sometimes returns an HTML error page with HTTP 200.
    # A real CSV response begins with the ADDRESS header row; an error page is a few
    # hundred bytes of HTML. Detect and raise so sync_log records a failure instead
    # of silently treating it as a 0-row success.
    head = text.lstrip()[:200].upper()
    if "WE HAD AN ERROR" in head or "ERROR EXECUTING" in head or head.startswith("<"):
        raise RuntimeError(
            f"Upstream feed returned error page ({len(text)} bytes): {text[:200]!r}"
        )
    if not ("ADDRESS" in head[:20]):
        raise RuntimeError(
            f"Upstream feed returned unexpected content ({len(text)} bytes): {text[:200]!r}"
        )
    return text


def parse_permits_csv(text: str) -> list[dict]:
    reader = csv.DictReader(io.StringIO(text))
    rows = []
    for raw in reader:
        address = (raw.get("ADDRESS") or "").strip()
        app_date = (raw.get("APPLICATIONDATE") or "").strip()
        desc = (raw.get("APPLICATIONDESCRIPTION") or "").strip()
        days = (raw.get("DAYSTOISSUE") or "").strip()
        cost = (raw.get("ESTPROJECTCOST") or "").strip()
        issue_date = (raw.get("ISSUEDATE") or "").strip()
        ptype = (raw.get("PROJECTTYPE") or "").strip()
        stype = (raw.get("STRUCTURETYPE") or "").strip()

        if not address or not issue_date:
            continue

        rows.append(
            {
                "dedup_key": dedup_key(address, app_date, desc, cost),
                "address": address,
                "application_date": app_date[:10] if app_date else None,
                "application_description": desc,
                "days_to_issue": int(days) if days.isdigit() else None,
                "est_project_cost": float(cost) if cost else None,
                "issue_date": issue_date[:10],
                "project_type": ptype or None,
                "structure_type": stype or None,
            }
        )
    return rows


# -----------------------------------------------------------------------------
# Sync
# -----------------------------------------------------------------------------
def sync_permits(notify_on_new: bool = True) -> tuple[int, int]:
    """
    Pull fresh CSV, upsert into SQLite, return (total_rows, new_permits).

    If notify_on_new is False, brand-new rows are inserted with notified=1 so
    they won't appear in the next digest (useful for initial seed).

    Auto-seed guard: if the DB is empty (first successful sync), we force
    notify_on_new=False regardless of caller. This protects against the case
    where the container's startup seed-sync failed (e.g. upstream feed was
    down) and a later scheduled sync would otherwise blast every historical
    row to subscribers on the next digest.
    """
    conn = get_db_standalone()
    try:
        existing_count = conn.execute("SELECT COUNT(*) FROM permits").fetchone()[0]
        if notify_on_new and existing_count == 0:
            log.info("DB is empty; forcing seed mode (notify_on_new=False)")
            notify_on_new = False

        text = fetch_permits_csv()
        rows = parse_permits_csv(text)
        new = 0
        for r in rows:
            cur = conn.execute(
                """INSERT OR IGNORE INTO permits
                (dedup_key, address, application_date, application_description,
                 days_to_issue, est_project_cost, issue_date, project_type,
                 structure_type, notified)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    r["dedup_key"], r["address"], r["application_date"],
                    r["application_description"], r["days_to_issue"],
                    r["est_project_cost"], r["issue_date"], r["project_type"],
                    r["structure_type"], 0 if notify_on_new else 1,
                ),
            )
            if cur.rowcount > 0:
                new += 1
        conn.execute(
            "INSERT INTO sync_log (total_rows, new_permits) VALUES (?, ?)",
            (len(rows), new),
        )
        conn.commit()
        log.info("Sync complete: %d total rows, %d new", len(rows), new)
        return len(rows), new
    except Exception as e:
        log.exception("Sync failed")
        conn.execute(
            "INSERT INTO sync_log (total_rows, new_permits, error) VALUES (0, 0, ?)",
            (str(e),),
        )
        conn.commit()
        return 0, 0
    finally:
        conn.close()


# -----------------------------------------------------------------------------
# Email
# -----------------------------------------------------------------------------
def send_email(to_addr: str, subject: str, html: str, text: str | None = None):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"STL Permits <{SMTP_USER}>"
    msg["To"] = to_addr
    if text:
        msg.attach(MIMEText(text, "plain"))
    msg.attach(MIMEText(html, "html"))
    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=30) as s:
        s.login(SMTP_USER, SMTP_PASS)
        s.send_message(msg)
    log.info("Sent '%s' to %s", subject[:60], to_addr)


def send_verify_email(sub: sqlite3.Row):
    verify_url = f"{BASE_URL}/verify/{sub['verify_token']}"
    unsub_url = f"{BASE_URL}/unsubscribe/{sub['unsubscribe_token']}"
    html = render_template(
        "email_verify.html", verify_url=verify_url, unsub_url=unsub_url
    )
    text = f"Click to verify your STL Permits subscription:\n\n{verify_url}\n\nUnsubscribe anytime: {unsub_url}\n"
    send_email(sub["email"], "Confirm your STL Permits subscription", html, text)


def render_digest_html(permits: list[sqlite3.Row], unsub_url: str, digest_date: str) -> str:
    return render_template(
        "email_digest.html",
        permits=permits,
        unsub_url=unsub_url,
        digest_date=digest_date,
        base_url=BASE_URL,
        permit_count=len(permits),
        total_cost=sum((p["est_project_cost"] or 0) for p in permits),
    )


def send_digest():
    """Email new-since-last-digest permits to all verified subscribers."""
    conn = get_db_standalone()
    try:
        # Find permits not yet notified
        permits = conn.execute(
            """SELECT * FROM permits
               WHERE notified = 0
               ORDER BY est_project_cost DESC NULLS LAST, issue_date DESC"""
        ).fetchall()
        if not permits:
            log.info("Digest: no new permits, skipping")
            return

        subs = conn.execute(
            "SELECT * FROM subscribers WHERE verified = 1"
        ).fetchall()
        if not subs:
            log.info("Digest: %d permits but no verified subs; marking notified", len(permits))
            ids = [p["id"] for p in permits]
            conn.executemany(
                "UPDATE permits SET notified = 1 WHERE id = ?",
                [(i,) for i in ids],
            )
            conn.commit()
            return

        digest_date = datetime.now().strftime("%A, %B %-d, %Y")
        sent = 0
        for sub in subs:
            unsub_url = f"{BASE_URL}/unsubscribe/{sub['unsubscribe_token']}"
            with app.app_context():
                html = render_digest_html(permits, unsub_url, digest_date)
            subject = f"STL Permits — {digest_date} — {len(permits)} new"
            try:
                send_email(sub["email"], subject, html)
                sent += 1
            except Exception:
                log.exception("Failed to send digest to %s", sub["email"])

        # Mark as notified
        ids = [p["id"] for p in permits]
        conn.executemany(
            "UPDATE permits SET notified = 1 WHERE id = ?",
            [(i,) for i in ids],
        )
        conn.execute(
            "INSERT INTO digest_log (permit_count, subscriber_count) VALUES (?, ?)",
            (len(permits), sent),
        )
        conn.commit()
        log.info("Digest sent: %d permits to %d subs", len(permits), sent)
    finally:
        conn.close()


# -----------------------------------------------------------------------------
# Routes
# -----------------------------------------------------------------------------
@app.route("/health")
def health():
    conn = get_db()
    total = conn.execute("SELECT COUNT(*) FROM permits").fetchone()[0]
    last = conn.execute(
        "SELECT ran_at, new_permits, error FROM sync_log ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return jsonify(
        {
            "ok": True,
            "total_permits": total,
            "last_sync": dict(last) if last else None,
        }
    )


@app.route("/")
def index():
    conn = get_db()
    # Latest 50 by issue date
    recent = conn.execute(
        """SELECT * FROM permits
           ORDER BY issue_date DESC, id DESC
           LIMIT 50"""
    ).fetchall()
    stats = conn.execute(
        """SELECT COUNT(*) AS total,
                  COALESCE(SUM(est_project_cost), 0) AS total_cost,
                  MAX(issue_date) AS latest_issue
           FROM permits"""
    ).fetchone()
    return render_template("index.html", recent=recent, stats=stats)


@app.route("/permit/<int:permit_id>")
def permit_detail(permit_id):
    conn = get_db()
    p = conn.execute("SELECT * FROM permits WHERE id = ?", (permit_id,)).fetchone()
    if not p:
        abort(404)
    return render_template("permit.html", p=p)


@app.route("/search")
def search():
    conn = get_db()
    q = (request.args.get("q") or "").strip()
    ptype = (request.args.get("type") or "").strip()
    min_cost = request.args.get("min_cost", type=float)
    page = request.args.get("page", 1, type=int)
    per_page = 50

    where = []
    params: list = []
    if q:
        where.append("(address LIKE ? OR application_description LIKE ?)")
        params.extend([f"%{q}%", f"%{q}%"])
    if ptype:
        where.append("project_type = ?")
        params.append(ptype)
    if min_cost:
        where.append("est_project_cost >= ?")
        params.append(min_cost)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    total = conn.execute(
        f"SELECT COUNT(*) FROM permits {where_sql}", params
    ).fetchone()[0]

    offset = (page - 1) * per_page
    rows = conn.execute(
        f"""SELECT * FROM permits {where_sql}
            ORDER BY issue_date DESC, id DESC
            LIMIT ? OFFSET ?""",
        [*params, per_page, offset],
    ).fetchall()
    return render_template(
        "search.html",
        rows=rows, total=total, q=q, ptype=ptype, min_cost=min_cost,
        page=page, per_page=per_page,
        pages=max(1, (total + per_page - 1) // per_page),
    )


@app.route("/subscribe", methods=["GET", "POST"])
def subscribe():
    if request.method == "GET":
        return render_template("subscribe.html")
    email = (request.form.get("email") or "").strip().lower()
    if not email or "@" not in email:
        return render_template("subscribe.html", error="Please enter a valid email."), 400
    conn = get_db()
    existing = conn.execute("SELECT * FROM subscribers WHERE email = ?", (email,)).fetchone()
    if existing:
        if existing["verified"]:
            return render_template("subscribe.html", message="You're already subscribed.")
        # Resend verify
        sub = existing
    else:
        verify_token = secrets.token_urlsafe(24)
        unsub_token = secrets.token_urlsafe(24)
        conn.execute(
            "INSERT INTO subscribers (email, verify_token, unsubscribe_token) VALUES (?, ?, ?)",
            (email, verify_token, unsub_token),
        )
        conn.commit()
        sub = conn.execute("SELECT * FROM subscribers WHERE email = ?", (email,)).fetchone()
    try:
        send_verify_email(sub)
    except Exception:
        log.exception("Could not send verify email")
        return render_template("subscribe.html", error="Email send failed. Try again later."), 500
    return render_template("subscribe.html", message="Check your email to confirm.")


@app.route("/verify/<token>")
def verify(token):
    conn = get_db()
    sub = conn.execute(
        "SELECT * FROM subscribers WHERE verify_token = ?", (token,)
    ).fetchone()
    if not sub:
        abort(404)
    conn.execute(
        "UPDATE subscribers SET verified = 1, verified_at = CURRENT_TIMESTAMP WHERE id = ?",
        (sub["id"],),
    )
    conn.commit()
    return render_template("verified.html", email=sub["email"])


@app.route("/unsubscribe/<token>")
def unsubscribe(token):
    conn = get_db()
    sub = conn.execute(
        "SELECT * FROM subscribers WHERE unsubscribe_token = ?", (token,)
    ).fetchone()
    if not sub:
        abort(404)
    conn.execute("DELETE FROM subscribers WHERE id = ?", (sub["id"],))
    conn.commit()
    return render_template("unsubscribed.html", email=sub["email"])


@app.route("/api/permits")
def api_permits():
    conn = get_db()
    limit = min(request.args.get("limit", 100, type=int), 500)
    rows = conn.execute(
        "SELECT * FROM permits ORDER BY issue_date DESC, id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return jsonify([dict(r) for r in rows])


# -----------------------------------------------------------------------------
# Template helpers
# -----------------------------------------------------------------------------
@app.template_filter("money")
def fmt_money(v):
    if v is None:
        return "—"
    try:
        return f"${float(v):,.0f}"
    except Exception:
        return "—"


@app.template_filter("pretty_date")
def fmt_date(v):
    if not v:
        return "—"
    try:
        return datetime.fromisoformat(str(v)[:10]).strftime("%b %-d, %Y")
    except Exception:
        return str(v)


# -----------------------------------------------------------------------------
# Background scheduler
# -----------------------------------------------------------------------------
def scheduler_loop():
    # First, make sure we have rows so the site isn't empty; don't blast digests
    conn = get_db_standalone()
    total = conn.execute("SELECT COUNT(*) FROM permits").fetchone()[0]
    conn.close()
    if total == 0:
        log.info("Initial seed: pulling CSV with notify_on_new=False")
        sync_permits(notify_on_new=False)

    # Periodic sync
    schedule.every(SYNC_INTERVAL_HOURS).hours.do(sync_permits, notify_on_new=True)
    # Daily digest
    schedule.every().day.at(f"{DIGEST_HOUR:02d}:00").do(send_digest)

    while True:
        try:
            schedule.run_pending()
        except Exception:
            log.exception("Scheduler tick failed")
        time.sleep(30)


# -----------------------------------------------------------------------------
# Startup
# -----------------------------------------------------------------------------
init_db()
# Start background thread once per process
_scheduler_started = False
_scheduler_lock = threading.Lock()

def _ensure_scheduler():
    global _scheduler_started
    with _scheduler_lock:
        if _scheduler_started:
            return
        t = threading.Thread(target=scheduler_loop, daemon=True, name="scheduler")
        t.start()
        _scheduler_started = True
        log.info("Scheduler thread started")

_ensure_scheduler()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=False)
