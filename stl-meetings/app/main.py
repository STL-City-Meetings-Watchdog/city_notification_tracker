#!/usr/bin/env python3
import os, re, sqlite3, hashlib, smtplib, logging, threading, time
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

def init_db():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    PDF_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS meetings (id INTEGER PRIMARY KEY AUTOINCREMENT, uid TEXT UNIQUE, title TEXT, description TEXT, location TEXT, start_time DATETIME, end_time DATETIME, event_url TEXT, sponsor TEXT, contact_name TEXT, contact_email TEXT, contact_phone TEXT, created_at DATETIME DEFAULT CURRENT_TIMESTAMP, updated_at DATETIME DEFAULT CURRENT_TIMESTAMP, notified BOOLEAN DEFAULT 0)''')
    c.execute('''CREATE TABLE IF NOT EXISTS documents (id INTEGER PRIMARY KEY AUTOINCREMENT, meeting_id INTEGER, doc_type TEXT, original_url TEXT, local_path TEXT, filename TEXT, extracted_text TEXT, created_at DATETIME DEFAULT CURRENT_TIMESTAMP, FOREIGN KEY (meeting_id) REFERENCES meetings(id))''')
    c.execute('''CREATE TABLE IF NOT EXISTS subscribers (id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT UNIQUE, boards TEXT, verified BOOLEAN DEFAULT 0, verify_token TEXT, unsubscribe_token TEXT, created_at DATETIME DEFAULT CURRENT_TIMESTAMP)''')
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
        boards = sub['boards']
        if boards and boards != 'all':
            board_list = [b.strip().lower() for b in boards.split(',')]
            if not any(b in (meeting['sponsor'] or '').lower() for b in board_list): continue
        send_email(sub['email'], subject, html_body.replace('{email}', sub['email']), attachments)


def check_upcoming_documents():
    """Re-scrape meetings in next 45 days for new documents"""
    logger.info('Checking meetings in next 45 days for new documents...')
    conn = get_db()
    c = conn.cursor()
    
    c.execute("""
        SELECT id, title, event_url, sponsor FROM meetings 
        WHERE start_time > datetime('now') 
        AND start_time < datetime('now', '+45 days')
        AND event_url IS NOT NULL
    """)
    meetings = c.fetchall()
    logger.info(f'Found {len(meetings)} meetings in next 45 days')
    
    new_docs_found = []
    
    for m in meetings:
        mid, title, url, sponsor = m[0], m[1], m[2], m[3]
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
                new_docs_found.append({"meeting_id": mid, "title": title, "filename": filename, "local_path": local_path, "sponsor": sponsor})
    
    conn.commit()
    
    if new_docs_found:
        c.execute('SELECT * FROM subscribers WHERE verified=1')
        subscribers = [dict(row) for row in c.fetchall()]
        
        for doc_info in new_docs_found:
            subject = "New Document: " + doc_info["title"][:50]
            html_body = "<html><body style=\"font-family:Arial\"><h2 style=\"color:#1a365d\">New Document Added</h2><p><b>Meeting:</b> " + doc_info["title"] + "</p><p><b>New Document:</b> " + doc_info["filename"] + "</p><p><a href=\"" + BASE_URL + "/meeting/" + str(doc_info["meeting_id"]) + "\" style=\"background:#c03221;color:white;padding:10px 20px;text-decoration:none\">View Meeting</a></p><hr><p style=\"font-size:12px;color:#666\"><a href=\"" + BASE_URL + "/unsubscribe?email={email}\">Unsubscribe</a></p></body></html>"
            
            filepath = PDF_DIR / doc_info["local_path"]
            attachments = [(str(filepath), doc_info["filename"])] if filepath.exists() else []
            
            for sub in subscribers:
                boards = sub["boards"]
                if boards and boards != "all":
                    board_list = [b.strip().lower() for b in boards.split(",")]
                    if not any(b in (doc_info["sponsor"] or "").lower() for b in board_list):
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
        c.execute('SELECT * FROM subscribers WHERE verified=1')
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
    if request.method == 'POST':
        
	email = request.form.get('email','').strip().lower()
        boards = request.form.get('boards','all')
        if not email or '@' not in email:
            return render_template('subscribe.html', error="Invalid email")
        vt = hashlib.sha256(f"{email}{datetime.now()}".encode()).hexdigest()[:32]
        conn = get_db()
        c = conn.cursor()
        try:
            c.execute('INSERT INTO subscribers (email,boards,verify_token) VALUES (?,?,?)', (email,boards,vt))
            conn.commit()
            send_email(email, "Verify STL Meetings subscription", f'<html><body><h2>Confirm</h2><p><a href="{BASE_URL}/verify?token={vt}" style="background:#c03221;color:white;padding:10px 20px;text-decoration:none">Confirm Subscription</a></p></body></html>')
        except: pass
	#this is a test email to see if it sends me an email correctly
	send_email(email, "Test email", f'<html><body><p>This is a test email to confirm delivery to {email}.</p></body></html>')
            print(f"[subscribe] Test email sent to: {email}")
        conn.close()
        return render_template('subscribe.html', success=True)
    return render_template('subscribe.html')

@app.route('/verify')
def verify():
    t = request.args.get('token','')
    conn = get_db()
    c = conn.cursor()
    c.execute('UPDATE subscribers SET verified=1 WHERE verify_token=?', (t,))
    conn.commit()
    ok = c.rowcount > 0
    conn.close()
    return render_template('verified.html') if ok else ("Invalid link", 400)

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

def run_scheduler():
    schedule.every(13).hours.do(sync_meetings)
    while True:
        schedule.run_pending()
        time.sleep(60)

init_db()

# Start scheduler thread (runs under gunicorn too)
scheduler_thread = threading.Thread(target=run_scheduler, daemon=True)
scheduler_thread.start()

if __name__ == '__main__':
    sync_meetings()
    app.run(host='0.0.0.0', port=8000)

from flask import send_from_directory

@app.route('/pdfs/<path:filepath>')
def serve_pdf(filepath):
    return send_from_directory('/app/pdfs', filepath)
