#!/usr/bin/env python3
#
# Copyright (C) 2026 city_notification_tracker contributors
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.

import os, re, sqlite3, secrets, smtplib, logging, threading, time, fcntl
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse, quote
import requests
from icalendar import Calendar
from bs4 import BeautifulSoup
from weasyprint import HTML
from flask import Flask, render_template, request, jsonify
import schedule

PROXY_URL = "https://stl-proxy.dan-f8a.workers.dev/?url="
ICAL_URLS = [
    "https://www.stlouis-mo.gov/customcf/endpoints/events/iCalGen.cfm?eventType=Meeting",
    "https://www.stlouis-mo.gov/customcf/endpoints/events/iCalGen.cfm?eventType=Aldermanic%20Committee%20Meeting",
    "https://www.stlouis-mo.gov/customcf/endpoints/events/iCalGen.cfm?eventType=Aldermanic%20General%20Meeting",
    "https://www.stlouis-mo.gov/customcf/endpoints/events/iCalGen.cfm?eventType=Aldermanic%20Special%20Committee%20Meeting",
]
BASE_URL = os.environ.get("BASE_URL", "https://stlmeetings.veiledprofits.com")
DATA_DIR = Path("/app/data")
PDF_DIR = Path("/app/pdfs")
DB_PATH = DATA_DIR / "meetings.db"
SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.hostinger.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", 465))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")


logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)
app = Flask(__name__, template_folder="/app/templates")

def proxy_get(url, timeout=30):
    """Fetch URL through Cloudflare proxy"""
    return requests.get(PROXY_URL + quote(url, safe=''), timeout=timeout)

def _migrate_subscribers_table(conn):
    """One-time migration from the old boards/verified columns to
    verified_boards/pending_boards (nothing is "verified" until a subscriber
    clicks their confirmation link, which copies pending_boards over into
    verified_boards - see /verify). Runs from every worker under the
    init_db() lock below, and each step is guarded by the schema it would
    change, so it's safe to resume after a crash partway through: SQLite
    auto-commits each ALTER TABLE the moment it runs (unlike the UPDATEs,
    which stay pending until the explicit commit), so a worker killed
    mid-migration can come back to a table with the new columns already
    added but not yet backfilled, or backfilled but not yet cleaned up -
    every step below checks for that instead of assuming all-or-nothing."""
    c = conn.cursor()

    def has_column(name):
        c.execute("PRAGMA table_info(subscribers)")
        return any(row[1] == name for row in c.fetchall())

    if not has_column("boards"):
        return  # already migrated, or a fresh DB created with the new schema
    logger.info("Migrating subscribers: boards/verified -> verified_boards/pending_boards")

    if not has_column("verified_boards"):
        c.execute("ALTER TABLE subscribers ADD COLUMN verified_boards TEXT NOT NULL DEFAULT ''")
    if not has_column("pending_boards"):
        c.execute("ALTER TABLE subscribers ADD COLUMN pending_boards TEXT NOT NULL DEFAULT ''")

    if has_column("verified"):
        # Already-verified subscribers keep their active subscription as-is,
        # and their old verify_token is nulled out - it was already spent
        # confirming this same boards value, so it must not be replayable to
        # re-confirm (which under the new schema would just copy '' over
        # verified_boards). Anyone still mid-signup keeps their in-flight
        # request as pending, so their existing (still-valid) verify_token
        # continues to work. Committed now, before either old column is
        # dropped, so this backfill is never lost even if the worker is
        # killed immediately after.
        c.execute("UPDATE subscribers SET verified_boards = boards, verify_token = NULL WHERE verified = 1")
        c.execute("UPDATE subscribers SET pending_boards = boards WHERE verified = 0")
        conn.commit()
        c.execute("ALTER TABLE subscribers DROP COLUMN verified")

    if has_column("boards"):
        c.execute("ALTER TABLE subscribers DROP COLUMN boards")

def init_db():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    PDF_DIR.mkdir(parents=True, exist_ok=True)
    # Every gunicorn worker calls init_db() on startup. A blocking flock
    # serializes them so only one performs the migration at a time; the rest
    # wait, then find _migrate_subscribers_table() already a no-op.
    with open(DATA_DIR / ".schema.lock", "w") as lock_fh:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS meetings (id INTEGER PRIMARY KEY AUTOINCREMENT, uid TEXT UNIQUE, title TEXT, description TEXT, location TEXT, start_time DATETIME, end_time DATETIME, event_url TEXT, sponsor TEXT, contact_name TEXT, contact_email TEXT, contact_phone TEXT, created_at DATETIME DEFAULT CURRENT_TIMESTAMP, updated_at DATETIME DEFAULT CURRENT_TIMESTAMP, notified BOOLEAN DEFAULT 0)''')
        c.execute('''CREATE TABLE IF NOT EXISTS documents (id INTEGER PRIMARY KEY AUTOINCREMENT, meeting_id INTEGER, doc_type TEXT, original_url TEXT, local_path TEXT, filename TEXT, extracted_text TEXT, created_at DATETIME DEFAULT CURRENT_TIMESTAMP, FOREIGN KEY (meeting_id) REFERENCES meetings(id))''')
        c.execute('''CREATE TABLE IF NOT EXISTS subscribers (id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT UNIQUE, verified_boards TEXT NOT NULL DEFAULT '', pending_boards TEXT NOT NULL DEFAULT '', verify_token TEXT, unsubscribe_token TEXT, created_at DATETIME DEFAULT CURRENT_TIMESTAMP)''')
        _migrate_subscribers_table(conn)
        conn.commit()
        conn.close()
    logger.info("Database initialized")

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def fetch_ical():
    all_events = []
    for url in ICAL_URLS:
        try:
            resp = proxy_get(url, timeout=60)
            resp.raise_for_status()
            cal = Calendar.from_ical(resp.content)
            for event in cal.walk("VEVENT"):
                all_events.append(event)
            logger.info(f"Fetched {len(list(cal.walk('VEVENT')))} from {url.split('=')[-1]}")
        except Exception as e:
            logger.error(f"Failed to fetch {url}: {e}")
    return all_events

def parse_event(event):
    uid = str(event.get('uid', ''))
    description = str(event.get('description', ''))
    sponsor, contact_name, contact_email, contact_phone = "", "", "", ""
    for line in description.split('\n'):
        if line.startswith('Sponsor:'): sponsor = line.replace('Sponsor:', '').strip()
        elif line.startswith('Contact Name:'): contact_name = line.replace('Contact Name:', '').strip()
        elif line.startswith('Email:'): contact_email = line.replace('Email:', '').strip()
        elif line.startswith('Phone:'): contact_phone = line.replace('Phone:', '').strip()
    return {'uid': uid, 'title': str(event.get('summary', '')), 'description': description, 'location': str(event.get('location', '')), 'start_time': event.get('dtstart').dt if event.get('dtstart') else None, 'end_time': event.get('dtend').dt if event.get('dtend') else None, 'event_url': str(event.get('url', '')), 'sponsor': sponsor, 'contact_name': contact_name, 'contact_email': contact_email, 'contact_phone': contact_phone}

def scrape_event_page(event_url):
    documents = []
    try:
        resp = proxy_get(event_url, timeout=30)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.content, 'html.parser')
        for link in soup.find_all('a', href=True):
            href = link['href']
            text = link.get_text().lower()
            if any(x in text for x in ['agenda', 'meeting materials', 'packet', 'minutes', 'board bill']) or '/documents/' in href or '/board-bills/' in href:
                full_url = urljoin(event_url, href)
                if href.endswith('.pdf'):
                    documents.append({'type': 'agenda', 'url': full_url, 'filename': os.path.basename(href)})
                elif '/documents/' in href:
                    documents.extend(scrape_documents_page(full_url))
                elif '/board-bills/' in href:
                    documents.extend(scrape_board_bill_page(full_url))
    except Exception as e:
        logger.error(f"Failed to scrape {event_url}: {e}")
    return documents

def scrape_documents_page(doc_url):
    documents = []
    try:
        resp = proxy_get(doc_url, timeout=30)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.content, 'html.parser')
        for link in soup.find_all('a', href=True):
            href = link['href']
            if href.endswith('.pdf'):
                documents.append({'type': 'document', 'url': urljoin(doc_url, href), 'filename': os.path.basename(href)})
    except Exception as e:
        logger.error(f"Failed to scrape docs {doc_url}: {e}")
    return documents

def scrape_board_bill_page(bill_url):
    documents = []
    try:
        resp = proxy_get(bill_url, timeout=30)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.content, 'html.parser')
        for link in soup.find_all('a', href=True):
            href = link['href']
            if href.endswith('.pdf'):
                full_url = urljoin(bill_url, href)
                filename = os.path.basename(href).replace('%20', '_')
                documents.append({'type': 'board_bill', 'url': full_url, 'filename': filename})
                break  # Just get the first/main PDF per bill
    except Exception as e:
        logger.error(f"Failed to scrape board bill {bill_url}: {e}")
    return documents

def download_document(url, meeting_id, doc_type):
    try:
        resp = proxy_get(url, timeout=60)
        resp.raise_for_status()
        meeting_dir = PDF_DIR / str(meeting_id)
        meeting_dir.mkdir(parents=True, exist_ok=True)
        filename = os.path.basename(urlparse(url).path) or "document.pdf"
        local_path = meeting_dir / filename
        with open(local_path, 'wb') as f:
            f.write(resp.content)
        return str(local_path.relative_to(PDF_DIR)), filename
    except Exception as e:
        logger.error(f"Failed to download {url}: {e}")
        return None, None

def generate_meeting_pdf(meeting):
    meeting_dir = PDF_DIR / str(meeting['id'])
    meeting_dir.mkdir(parents=True, exist_ok=True)
    start = meeting['start_time']
    if isinstance(start, str):
        try: start = datetime.fromisoformat(start.replace('Z', '+00:00'))
        except: pass
    date_str = start.strftime('%B %d, %Y at %I:%M %p') if hasattr(start, 'strftime') else str(start) if start else 'TBD'
    search_date = start.strftime('%B %d') if hasattr(start, 'strftime') else ''
    # Add CivicClerk note for Aldermanic meetings
    civicclerk_note = ''
    if 'Committee' in meeting['title'] or 'Aldermanic' in meeting['title']:
        civicclerk_note = f'''<div style="background:#fff3cd;border:1px solid #ffc107;padding:15px;margin:20px 0;border-radius:5px">
        <p style="margin:0 0 10px 0"><b>📋 Agenda &amp; Packet:</b></p>
        <p style="margin:0">For the full agenda and meeting packet, visit the CivicClerk portal:</p>
        <p style="margin:10px 0"><a href="https://stlouismo.portal.civicclerk.com/">https://stlouismo.portal.civicclerk.com/</a></p>
        <p style="margin:0;font-size:13px;color:#666">Search for: <b>"{meeting['title']}"</b> on <b>{search_date}</b></p>
        </div>'''
    html_content = f'''<!DOCTYPE html><html><head><meta charset="utf-8"><style>body{{font-family:Arial,sans-serif;margin:40px}}h1{{color:#1a365d;border-bottom:2px solid #c03221;padding-bottom:10px}}.meta{{background:#f5f5f5;padding:15px;margin:20px 0}}</style></head><body><h1>{meeting['title']}</h1><div class="meta"><p><b>Date:</b> {date_str}</p><p><b>Location:</b> {meeting['location'] or 'See details'}</p><p><b>Sponsor:</b> {meeting['sponsor'] or 'N/A'}</p></div>{civicclerk_note}<p><b>Official Page:</b> <a href="{meeting['event_url']}">{meeting['event_url']}</a></p><p style="margin-top:40px;font-size:12px;color:#666">Generated by STL Meetings Archive - {BASE_URL}</p></body></html>'''
    pdf_path = meeting_dir / "notice.pdf"
    try:
        HTML(string=html_content).write_pdf(str(pdf_path))
        return str(pdf_path.relative_to(PDF_DIR))
    except Exception as e:
        logger.error(f"Failed to generate PDF: {e}")
        return None

def send_email(to_email, subject, html_body, attachments=None):
    if not SMTP_USER or not SMTP_PASS:
        logger.warning("SMTP not configured")
        return False
    try:
        msg = MIMEMultipart('mixed')
        msg['Subject'] = subject
        msg['From'] = SMTP_USER
        msg['To'] = to_email
        msg.attach(MIMEText(html_body, 'html'))
        if attachments:
            from email.mime.application import MIMEApplication
            for filepath, filename in attachments:
                try:
                    with open(filepath, 'rb') as f:
                        part = MIMEApplication(f.read(), Name=filename)
                    part['Content-Disposition'] = f'attachment; filename="{filename}"'
                    msg.attach(part)
                except Exception as e:
                    logger.error(f"Failed to attach {filename}: {e}")
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT) as server:
            server.login(SMTP_USER, SMTP_PASS)
            server.send_message(msg)
        logger.info(f"Email sent to {to_email}")
        return True
    except Exception as e:
        logger.error(f"Failed to send email: {e}")
        return False

# --- Board / topic filtering -------------------------------------------------
# Subscribers can narrow to a board group on the subscribe form. The iCal feed
# does NOT populate a usable "sponsor" field (it is empty on every event), so we
# match the subscriber's chosen keywords against the meeting TITLE instead (see
# boards_match). Matching leans generous: for a public-notice service,
# over-notifying is far better than silently dropping a subscriber.
BOARD_MATCH = {
    'aldermen': [r'\balderman', r'\baldermen\b', r'\baldermanic\b', r'board of aldermen'],
    'sldc':     [r'\bsldc\b', r'st\.? ?louis development', r'development corporation'],
    'lcra':     [r'\blcra\b', r'land clearance'],
    'piea':     [r'\bpiea\b', r'planned industrial'],
    'tif':      [r'\btif\b', r'tax increment'],
    'zoning':   [r'\bzoning\b'],
    'planning': [r'\bplanning\b'],
}
VALID_BOARDS = set(BOARD_MATCH.keys()) | {'all'}
BOARD_LABELS = {
    'all': 'All meetings',
    'aldermen': 'Board of Aldermen',
    'sldc': 'SLDC',
    'lcra': 'LCRA',
    'piea': 'PIEA',
    'tif': 'TIF',
    'zoning': 'Zoning',
    'planning': 'Planning',
}

# Category groupings for the subscribe form's checkboxes, and for grouping
# the verify-confirm/verified pages' display to match. Single source of
# truth - both subscribe.html and the /verify route pull their
# board-to-group mapping from here.
# Invariant: every non-'all' key in BOARD_MATCH (i.e. every token VALID_BOARDS
# allows) must appear in exactly one group below - describe_boards() assumes
# this and does not handle a token that isn't in any group.
BOARD_GROUPS = [
    ('Board of Aldermen', ['aldermen']),
    ('Development', ['sldc', 'lcra', 'piea', 'tif']),
    ('Land Use', ['zoning', 'planning']),
]
# Groups whose member boards get spelled out in parentheses, e.g.
# "Development (SLDC, TIF)". Everything else just shows the group name -
# right for "Board of Aldermen" (nothing more to say).
GROUPS_WITH_BREAKDOWN = {'Development', 'Land Use'}

def describe_boards(boards):
    """Turn a stored boards string into a list of human-readable lines, one
    per matched category in BOARD_GROUPS, for display on the verify-confirm
    and verified pages.

    A group in GROUPS_WITH_BREAKDOWN names which of its boards were picked;
    every other group just shows its name.

    Examples:
        describe_boards('all')                    -> ['All meetings']
        describe_boards('aldermen')                -> ['Board of Aldermen']
        describe_boards('sldc,tif')                -> ['Development (SLDC, TIF)']
        describe_boards('aldermen,sldc,zoning')    -> ['Board of Aldermen',
                                                         'Development (SLDC)',
                                                         'Land Use (Zoning)']
    """
    selected = {t for t in boards.split(',') if t}
    if 'all' in selected:
        return [BOARD_LABELS['all']]
    if not selected:
        # Not reachable from /subscribe (which always stores at least one
        # token), but an empty string here means no notifications at all -
        # every notification query filters on verified_boards != '' - so
        # don't render it as a blank list that looks like a page bug.
        return ['(no meeting types selected)']

    lines = []
    for group_name, group_tokens in BOARD_GROUPS:
        picked = [t for t in group_tokens if t in selected]
        if not picked:
            continue
        if group_name in GROUPS_WITH_BREAKDOWN:
            picked_labels = ', '.join(BOARD_LABELS[t] for t in picked)
            lines.append(f"{group_name} ({picked_labels})")
        else:
            lines.append(group_name)
    return lines

# (label, data-board value) pairs for the subscribe form's checkboxes, one
# per BOARD_GROUPS entry. Reuses describe_boards() on each group's full token
# set so a checkbox's label always matches what the verify pages would show
# for that same full selection, instead of the wording being written out
# separately by hand. Computed once at import time - BOARD_GROUPS and
# BOARD_LABELS are both static.
BOARD_CHECKBOX_OPTIONS = [
    (describe_boards(','.join(tokens))[0], ','.join(tokens))
    for _, tokens in BOARD_GROUPS
]

def boards_match(boards, title, description=''):
    """Whether a subscriber with this `boards` selection should receive a meeting.
    Keyword groups are matched against the TITLE only — the iCal `sponsor` field
    is always empty, and matching the full description drags in boilerplate (e.g.
    the Board of Aldermen rules text names "St. Louis Development Corporation",
    which would spam development subscribers with every BOA meeting). Aldermanic
    standing committees don't carry "aldermen" in their title but identify
    themselves in the description ("...this board of aldermen committee
    meeting..."), so 'aldermen' also matches on that phrase. 'all' or empty
    => always True. An unrecognized token never matches (subscribe-time
    validation should prevent these; this is just a safety net for old rows)."""
    if not boards or boards.strip().lower() == 'all':
        return True
    title_l = (title or '').lower()
    desc_l = (description or '').lower()
    for tok in [t.strip().lower() for t in boards.split(',') if t.strip()]:
        pats = BOARD_MATCH.get(tok)
        if pats is None:
            continue
        if any(re.search(p, title_l) for p in pats):
            return True
        if tok == 'aldermen' and re.search(r'board\s+of\s+aldermen\s+committee', desc_l):
            return True
    return False

def send_meeting_notification(meeting, subscribers):
    start = meeting['start_time']
    if isinstance(start, str):
        try: start = datetime.fromisoformat(start.replace('Z', '+00:00'))
        except: pass
    date_str = start.strftime('%B %d, %Y at %I:%M %p') if hasattr(start, 'strftime') else str(start) if start else 'TBD'
    subject = f"STL Meeting: {meeting['title']}"
    html_body = f'''<html><body style="font-family:Arial"><h2 style="color:#1a365d">{meeting['title']}</h2><p><b>Date:</b> {date_str}</p><p><b>Location:</b> {meeting['location'] or 'See details'}</p><p><a href="{BASE_URL}/meeting/{meeting['id']}" style="background:#c03221;color:white;padding:10px 20px;text-decoration:none">View Meeting</a></p><hr><p style="font-size:12px;color:#666"><a href="{BASE_URL}/unsubscribe?email={{email}}">Unsubscribe</a></p></body></html>'''
    # Gather PDF attachments
    attachments = []
    conn2 = get_db()
    c2 = conn2.cursor()
    c2.execute('SELECT local_path, filename FROM documents WHERE meeting_id=?', (meeting['id'],))
    for row in c2.fetchall():
        filepath = PDF_DIR / row[0]
        if filepath.exists():
            attachments.append((str(filepath), row[1]))
    conn2.close()
    
    for sub in subscribers:
        if boards_match(sub['verified_boards'], meeting.get('title'), meeting.get('description')):
            send_email(sub['email'], subject, html_body.replace('{email}', sub['email']), attachments)


def check_upcoming_documents():
    """Re-scrape meetings in next 45 days for new documents"""
    logger.info('Checking meetings in next 45 days for new documents...')
    conn = get_db()
    c = conn.cursor()
    
    c.execute("""
        SELECT id, title, event_url, sponsor, description FROM meetings
        WHERE start_time > datetime('now')
        AND start_time < datetime('now', '+45 days')
        AND event_url IS NOT NULL
    """)
    meetings = c.fetchall()
    logger.info(f'Found {len(meetings)} meetings in next 45 days')
    
    new_docs_found = []
    
    for m in meetings:
        mid, title, url, sponsor, description = m[0], m[1], m[2], m[3], m[4]
        docs = scrape_event_page(url)
        
        for doc in docs:
            c.execute('SELECT id FROM documents WHERE meeting_id=? AND filename=?', (mid, doc["filename"]))
            if c.fetchone():
                continue
            
            local_path, filename = download_document(doc["url"], mid, doc["type"])
            if local_path:
                c.execute('INSERT INTO documents (meeting_id,doc_type,original_url,local_path,filename) VALUES (?,?,?,?,?)',
                    (mid, doc["type"], doc["url"], local_path, filename))
                logger.info(f'New document for meeting {mid}: {filename}')
                new_docs_found.append({"meeting_id": mid, "title": title, "filename": filename, "local_path": local_path, "sponsor": sponsor, "description": description})
    
    conn.commit()
    
    if new_docs_found:
        c.execute("SELECT * FROM subscribers WHERE verified_boards != ''")
        subscribers = [dict(row) for row in c.fetchall()]
        
        for doc_info in new_docs_found:
            subject = "New Document: " + doc_info["title"][:50]
            html_body = "<html><body style=\"font-family:Arial\"><h2 style=\"color:#1a365d\">New Document Added</h2><p><b>Meeting:</b> " + doc_info["title"] + "</p><p><b>New Document:</b> " + doc_info["filename"] + "</p><p><a href=\"" + BASE_URL + "/meeting/" + str(doc_info["meeting_id"]) + "\" style=\"background:#c03221;color:white;padding:10px 20px;text-decoration:none\">View Meeting</a></p><hr><p style=\"font-size:12px;color:#666\"><a href=\"" + BASE_URL + "/unsubscribe?email={email}\">Unsubscribe</a></p></body></html>"
            
            filepath = PDF_DIR / doc_info["local_path"]
            attachments = [(str(filepath), doc_info["filename"])] if filepath.exists() else []
            
            for sub in subscribers:
                if not boards_match(sub["verified_boards"], doc_info["title"], doc_info.get("description", "")):
                    continue
                send_email(sub["email"], subject, html_body.replace("{email}", sub["email"]), attachments)
    
    conn.close()
    logger.info(f'Document check done. {len(new_docs_found)} new documents found.')

def sync_meetings():
    logger.info("Starting sync...")
    events = fetch_ical()
    if not events: return
    conn = get_db()
    c = conn.cursor()
    new_meetings = []
    for event in events:
        data = parse_event(event)
        if not data['uid']: continue
        c.execute('SELECT id FROM meetings WHERE uid=?', (data['uid'],))
        existing = c.fetchone()
        if existing:
            c.execute('UPDATE meetings SET title=?,description=?,location=?,start_time=?,end_time=?,event_url=?,sponsor=?,contact_name=?,contact_email=?,contact_phone=?,updated_at=CURRENT_TIMESTAMP WHERE uid=?', (data['title'],data['description'],data['location'],data['start_time'],data['end_time'],data['event_url'],data['sponsor'],data['contact_name'],data['contact_email'],data['contact_phone'],data['uid']))
        else:
            c.execute('INSERT INTO meetings (uid,title,description,location,start_time,end_time,event_url,sponsor,contact_name,contact_email,contact_phone) VALUES (?,?,?,?,?,?,?,?,?,?,?)', (data['uid'],data['title'],data['description'],data['location'],data['start_time'],data['end_time'],data['event_url'],data['sponsor'],data['contact_name'],data['contact_email'],data['contact_phone']))
            c.execute('SELECT * FROM meetings WHERE id=?', (c.lastrowid,))
            new_meetings.append(dict(c.fetchone()))
            logger.info(f"New: {data['title']}")
    conn.commit()
    for meeting in new_meetings:
        pdf_path = generate_meeting_pdf(meeting)
        if pdf_path:
            c.execute('INSERT INTO documents (meeting_id,doc_type,local_path,filename) VALUES (?,"notice",?,"notice.pdf")', (meeting['id'],pdf_path))
        if meeting['event_url']:
            for doc in scrape_event_page(meeting['event_url']):
                local_path, filename = download_document(doc['url'], meeting['id'], doc['type'])
                if local_path:
                    c.execute('INSERT INTO documents (meeting_id,doc_type,original_url,local_path,filename) VALUES (?,?,?,?,?)', (meeting['id'],doc['type'],doc['url'],local_path,filename))
        c.execute('UPDATE meetings SET notified=1 WHERE id=?', (meeting['id'],))
    conn.commit()
    if new_meetings:
        c.execute("SELECT * FROM subscribers WHERE verified_boards != ''")
        subs = [dict(row) for row in c.fetchall()]
        for meeting in new_meetings:
            send_meeting_notification(meeting, subs)
    conn.close()
    logger.info(f"Sync done. {len(new_meetings)} new.")

@app.route('/')
def index():
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM meetings WHERE start_time>=date('now') ORDER BY start_time LIMIT 50")
    upcoming = [dict(r) for r in c.fetchall()]
    c.execute("SELECT * FROM meetings WHERE start_time<date('now') ORDER BY start_time DESC LIMIT 50")
    past = [dict(r) for r in c.fetchall()]
    conn.close()
    return render_template('index.html', upcoming=upcoming, past=past)


@app.route('/meeting/<int:mid>')
def meeting_detail(mid):
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT * FROM meetings WHERE id=?', (mid,))
    m = c.fetchone()
    if not m: return "Not found", 404
    m = dict(m)
    c.execute('SELECT * FROM documents WHERE meeting_id=?', (mid,))
    docs = [dict(r) for r in c.fetchall()]
    conn.close()
    return render_template('meeting.html', meeting=m, documents=docs)

@app.route('/search')
def search():
    q = request.args.get('q', '')
    if not q: return render_template('search.html', results=[], query='')
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT * FROM meetings WHERE title LIKE ? OR description LIKE ? OR sponsor LIKE ? ORDER BY start_time DESC LIMIT 100', (f'%{q}%',f'%{q}%',f'%{q}%'))
    results = [dict(r) for r in c.fetchall()]
    conn.close()
    return render_template('search.html', results=results, query=q)


@app.route('/subscribe', methods=['GET','POST'])
def subscribe():
    if request.method == 'GET':
        return render_template('subscribe.html', checkbox_options=BOARD_CHECKBOX_OPTIONS)

    email = request.form.get('email','').strip().lower()
    boards = request.form.get('boards','all')
    if not email or '@' not in email:
        return render_template('subscribe.html', checkbox_options=BOARD_CHECKBOX_OPTIONS, error="Invalid email")
    tokens = [t.strip().lower() for t in boards.split(',') if t.strip()]
    if any(t not in VALID_BOARDS for t in tokens):
        return render_template('subscribe.html', checkbox_options=BOARD_CHECKBOX_OPTIONS, error="Invalid board selection")
    boards = ','.join(tokens) or 'all'
    # Must be unguessable: this token is the ONLY thing standing between a
    # stranger and someone else's verified_boards. The old sha256(email +
    # timestamp) was derived entirely from values an attacker who triggered
    # the signup already knows, so it was brute-forceable offline.
    vt = secrets.token_urlsafe(32)

    conn = get_db()
    c = conn.cursor()
    # Whatever they selected becomes the pending request, replacing any
    # earlier unconfirmed one. This never touches verified_boards, so no
    # one's active subscription changes until they click the link below.
    c.execute(
        """
        INSERT INTO subscribers (email, pending_boards, verify_token)
        VALUES (?, ?, ?)
        ON CONFLICT(email) DO UPDATE SET pending_boards=excluded.pending_boards, verify_token=excluded.verify_token
        """,
        (email, boards, vt),
    )
    conn.commit()
    conn.close()

    send_email(
        email,
        "Verify STL Meetings subscription",
        f'<html><body><h2>Confirm your subscription</h2><p>Click below to review and confirm what you signed up for.</p><p><a href="{BASE_URL}/verify?token={vt}" style="color:#c03221">Review your subscription request</a></p></body></html>',
    )
    return render_template("subscribe.html", success=True)

@app.route('/verify', methods=['GET', 'POST'])
def verify():
    token = request.values.get("token", "")
    conn = get_db()
    c = conn.cursor()

    if request.method == 'GET':
        # Side-effect-free: safe for an email-security link scanner to
        # prefetch, since it only reads state and never confirms anything.
        # Confirming requires an actual POST from the button below, which a
        # scanner fetching this page won't trigger.
        c.execute(
            "SELECT pending_boards FROM subscribers WHERE verify_token=?", (token,)
        )
        row = c.fetchone()
        conn.close()
        if not row:
            return ("Invalid or already-used link", 400)
        return render_template(
            "verify_confirm.html",
            token=token,
            boards=describe_boards(row["pending_boards"]),
        )

    # POST: verify_token is nulled out below once consumed, so a replay
    # (double-submit, or a confirm page left open in two tabs) matches no
    # rows instead of collapsing the just-activated subscription back out.
    c.execute("SELECT pending_boards FROM subscribers WHERE verify_token=?", (token,))
    row = c.fetchone()
    if not row:
        conn.close()
        return ("Invalid or already-used link", 400)
    pending = row['pending_boards']
    c.execute(
        "UPDATE subscribers SET verified_boards = pending_boards, pending_boards = '', verify_token = NULL "
        "WHERE verify_token=?",
        (token,),
    )
    if c.rowcount == 0:
        conn.close()
        return ("Invalid or already-used link", 400)
    conn.commit()
    conn.close()
    return render_template('verified.html', boards=describe_boards(pending))

@app.route('/unsubscribe')
def unsubscribe():
    conn = get_db()
    c = conn.cursor()
    c.execute('DELETE FROM subscribers WHERE email=?', (request.args.get('email',''),))
    conn.commit()
    conn.close()
    return render_template('unsubscribed.html')

@app.route('/api/meetings')
def api_meetings():
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT * FROM meetings ORDER BY start_time DESC LIMIT 100')
    m = [dict(r) for r in c.fetchall()]
    conn.close()
    return jsonify(m)

def scheduled_job():
    """Full refresh: pull new meetings (which emails subscribers about brand-new
    meetings), then re-scrape upcoming meetings for newly posted documents (which
    emails subscribers about those). Run sequentially so the two never write to
    the SQLite DB concurrently."""
    try:
        sync_meetings()
    except Exception as e:
        logger.error(f"sync_meetings failed: {e}")
    try:
        check_upcoming_documents()
    except Exception as e:
        logger.error(f"check_upcoming_documents failed: {e}")

def run_scheduler():
    schedule.every(12).hours.do(scheduled_job)
    while True:
        schedule.run_pending()
        time.sleep(60)

_scheduler_lock_fh = None
def acquire_scheduler_lock():
    """Only ONE gunicorn worker may run the scheduler (each worker imports this
    module). The worker that wins an exclusive flock on a file in the shared data
    volume runs it; if that worker dies the OS releases the lock and a
    replacement worker picks it up on its next start.
    Known gap: this only covers a worker that DIES. A worker that hangs while
    staying alive (e.g. deadlocked) keeps the fd open forever, so no other
    worker can take over the scheduler and nothing fails loudly - gunicorn's
    own --timeout only recycles a worker whose main request-handling thread
    stalls, not a stuck background thread. Accepted risk for now."""
    global _scheduler_lock_fh
    try:
        fh = open(DATA_DIR / ".scheduler.lock", "w")
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        _scheduler_lock_fh = fh  # keep the handle for the process lifetime
        return True
    except OSError:
        return False

init_db()

# Start the scheduler in exactly one worker.
if acquire_scheduler_lock():
    logger.info("Scheduler lock acquired - starting scheduler in this worker")
    scheduler_thread = threading.Thread(target=run_scheduler, daemon=True)
    scheduler_thread.start()
else:
    logger.info("Scheduler lock held by another worker - not scheduling here")

if __name__ == '__main__':
    sync_meetings()
    app.run(host='0.0.0.0', port=8000)

from flask import send_from_directory

@app.route('/pdfs/<path:filepath>')
def serve_pdf(filepath):
    return send_from_directory('/app/pdfs', filepath)
