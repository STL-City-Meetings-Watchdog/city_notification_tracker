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

import os
import re
import json
import sqlite3
import smtplib
import secrets
import schedule
import time
import threading
import logging
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from flask import Flask, render_template, request, redirect, url_for, jsonify, abort
from bs4 import BeautifulSoup
import requests


logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__)

# ── Config ─────────────────────────────────────────────────────────────────────
DB_PATH      = '/app/data/permits.db'
BASE_URL     = os.environ.get('BASE_URL', 'http://localhost:8000')
SMTP_HOST    = os.environ.get('SMTP_HOST', 'smtp.hostinger.com')
SMTP_PORT    = int(os.environ.get('SMTP_PORT', 465))
SMTP_USER    = os.environ.get('SMTP_USER', '')
SMTP_PASS    = os.environ.get('SMTP_PASS', '')
# Optional Cloudflare worker proxy for dnr.mo.gov (same pattern as stl-meetings)
# e.g. https://your-worker.workers.dev/?url=
CF_PROXY_URL = os.environ.get('CF_PROXY_URL', '')

DNR_BASE      = 'https://dnr.mo.gov'
ISSUED_HTML   = '/air/business-industry/permits/issued'
PENDING_HTML  = '/air/business-industry/permits/pending'

MO_COUNTIES = [
    'Adair','Andrew','Atchison','Audrain','Barry','Barton','Bates','Benton',
    'Bollinger','Boone','Buchanan','Butler','Caldwell','Callaway','Camden',
    'Cape Girardeau','Carroll','Carter','Cass','Cedar','Chariton','Christian',
    'Clark','Clay','Clinton','Cole','Cooper','Crawford','Dade','Dallas',
    'Daviess','DeKalb','Dent','Douglas','Dunklin','Franklin','Gasconade',
    'Gentry','Greene','Grundy','Harrison','Henry','Hickory','Holt','Howard',
    'Howell','Iron','Jackson','Jasper','Jefferson','Johnson','Knox','Laclede',
    'Lafayette','Lawrence','Lewis','Lincoln','Linn','Livingston','Macon',
    'Madison','Maries','Marion','McDonald','Mercer','Miller','Mississippi',
    'Moniteau','Monroe','Montgomery','Morgan','New Madrid','Newton','Nodaway',
    'Oregon','Osage','Ozark','Pemiscot','Perry','Pettis','Phelps','Pike',
    'Platte','Polk','Pulaski','Putnam','Ralls','Randolph','Ray','Reynolds',
    'Ripley','Saline','Schuyler','Scotland','Scott','Shannon','Shelby',
    'St. Charles','St. Clair','St. Francois','St. Louis','St. Louis City',
    'Ste. Genevieve','Stoddard','Stone','Sullivan','Taney','Texas','Vernon',
    'Warren','Washington','Wayne','Webster','Worth','Wright'
]


PERMIT_TYPE_GROUPS = {
    'Construction': 'Construction Permit',
    'Operating':    'Operating Permit',
    'Part 70':      'Part 70 Operating Permit',
    'Intermediate': 'Intermediate Operating Permit',
    'General':      'General Permit',
    'De Minimis':   'De Minimis',
}

# ── Database ────────────────────────────────────────────────────────────────────
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    c = conn.cursor()
    c.executescript('''
        CREATE TABLE IF NOT EXISTS permits (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            permit_number   TEXT UNIQUE,
            facility_name   TEXT,
            site_id         TEXT,
            city            TEXT,
            county          TEXT,
            permit_type     TEXT,
            date_issued     TEXT,
            date_issued_ts  DATETIME,
            dnr_url         TEXT,
            dnr_slug        TEXT,
            first_seen      DATETIME DEFAULT CURRENT_TIMESTAMP,
            notified        BOOLEAN  DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_permits_date   ON permits(date_issued_ts DESC);
        CREATE INDEX IF NOT EXISTS idx_permits_county ON permits(county);
        CREATE INDEX IF NOT EXISTS idx_permits_type   ON permits(permit_type);

        CREATE TABLE IF NOT EXISTS pending_permits (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            application_number  TEXT UNIQUE,
            facility_name       TEXT,
            site_id             TEXT,
            city                TEXT,
            county              TEXT,
            permit_type         TEXT,
            date_complete       TEXT,
            date_complete_ts    DATETIME,
            dnr_url             TEXT,
            first_seen          DATETIME DEFAULT CURRENT_TIMESTAMP,
            notified            BOOLEAN  DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_pending_date   ON pending_permits(date_complete_ts DESC);
        CREATE INDEX IF NOT EXISTS idx_pending_county ON pending_permits(county);

        CREATE TABLE IF NOT EXISTS subscribers (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            email               TEXT UNIQUE,
            county_filter       TEXT    DEFAULT 'all',
            permit_type_filter  TEXT    DEFAULT 'all',
            verified            BOOLEAN DEFAULT 0,
            verify_token        TEXT,
            unsubscribe_token   TEXT,
            created_at          DATETIME DEFAULT CURRENT_TIMESTAMP
        );
    ''')
    conn.commit()
    conn.close()
    logger.info("Database initialized")


# ── DNR HTML scraper ────────────────────────────────────────────────────────────
# NOTE: The DNR's JSON API (/api/1.0/...) does NOT paginate — it returns the
# same 10 records on every page regardless of the ?page= param. The HTML pages
# at /air/business-industry/permits/issued DO paginate correctly (40 per page,
# ~220 pages, ~8,800 total records). We scrape those instead.

SCRAPE_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    'Accept': 'text/html,application/xhtml+xml',
}

def _build_url(path):
    """Optionally route through Cloudflare proxy."""
    url = DNR_BASE + path
    if CF_PROXY_URL:
        import urllib.parse
        return CF_PROXY_URL + urllib.parse.quote(url, safe='')
    return url

def scrape_permits_page(page=0, order='field_date_issued', sort='desc'):
    """
    Fetch one HTML page of issued permits.
    Returns list of dicts with keys:
      facility_name, site_id, city, county, permit_type,
      permit_number, date_issued, date_issued_ts, dnr_url, dnr_slug
    """
    url = _build_url(ISSUED_HTML)
    try:
        r = requests.get(url, params={'page': page, 'order': order, 'sort': sort},
                         headers=SCRAPE_HEADERS, timeout=30)
        r.raise_for_status()
    except Exception as e:
        logger.error(f"Scrape page {page} failed: {e}")
        return None  # None = network error (don't stop sync)

    soup = BeautifulSoup(r.text, 'html.parser')
    rows = soup.select('table tbody tr')
    if not rows:
        return []  # Empty = past last page

    results = []
    for row in rows:
        cells = row.select('td')
        if len(cells) < 6:
            continue
        a           = cells[0].select_one('a')
        dnr_path    = a['href'] if a else ''
        facility    = a.get_text(strip=True) if a else cells[0].get_text(strip=True)
        site_id     = cells[1].get_text(strip=True)
        loc_text    = cells[2].get_text(separator=',', strip=True)
        loc_parts   = [p.strip() for p in loc_text.split(',') if p.strip()]
        city        = loc_parts[0] if len(loc_parts) >= 2 else ''
        county      = loc_parts[1] if len(loc_parts) >= 2 else (loc_parts[0] if loc_parts else '')
        permit_type = cells[3].get_text(strip=True)
        permit_num  = cells[4].get_text(strip=True)
        date_str    = cells[5].get_text(strip=True)

        date_ts = None
        try:
            date_ts = datetime.strptime(date_str, '%m/%d/%Y')
        except Exception:
            pass

        dnr_slug = dnr_path.lstrip('/')
        dnr_url  = f"{DNR_BASE}{dnr_path}" if dnr_path else ''

        if permit_num:
            results.append({
                'facility_name': facility, 'site_id': site_id,
                'city': city, 'county': county, 'permit_type': permit_type,
                'permit_number': permit_num, 'date_issued': date_str,
                'date_issued_ts': date_ts, 'dnr_url': dnr_url, 'dnr_slug': dnr_slug,
            })
    return results

# ── Sync engine ─────────────────────────────────────────────────────────────────
def sync_permits():
    """
    Scrape DNR HTML pages sorted newest-first.
    On initial run: pages all ~220 pages (~8,800 records, ~5 min).
    On subsequent runs: stops after 80 consecutive known permits (~2 pages).
    Returns count of new records.
    """
    logger.info("Permit sync started")
    conn = get_db()
    c    = conn.cursor()
    new_permits      = []
    page             = 0
    consecutive_known = 0
    errors           = 0

    while True:
        rows = scrape_permits_page(page=page)

        if rows is None:          # Network error
            errors += 1
            logger.warning(f"Page {page} network error ({errors} total)")
            if errors >= 5:
                logger.error("Too many errors, aborting sync")
                break
            time.sleep(5)
            continue

        if rows == []:            # Past the last page
            logger.info(f"No rows on page {page} — done")
            break

        page_new = 0
        for p in rows:
            c.execute('SELECT id FROM permits WHERE permit_number = ?', (p['permit_number'],))
            if c.fetchone():
                consecutive_known += 1
                continue

            consecutive_known = 0
            page_new += 1
            c.execute('''
                INSERT OR IGNORE INTO permits
                    (permit_number, facility_name, site_id, city, county,
                     permit_type, date_issued, date_issued_ts, dnr_url, dnr_slug)
                VALUES (:permit_number,:facility_name,:site_id,:city,:county,
                        :permit_type,:date_issued,:date_issued_ts,:dnr_url,:dnr_slug)
            ''', p)
            if c.lastrowid:
                new_permits.append({**p, 'id': c.lastrowid})

        conn.commit()
        logger.info(f"Page {page}: {page_new} new | {consecutive_known} consec. known | total new: {len(new_permits)}")

        # After seeding, stop early once we've seen 2 full pages of known records
        if page > 0 and consecutive_known >= 80:
            logger.info("80 consecutive known permits — incremental sync complete")
            break

        page += 1
        time.sleep(0.5)  # polite crawl delay

    conn.close()
    logger.info(f"Sync complete: {len(new_permits)} new permits")
    if new_permits:
        notify_subscribers(new_permits)
    return len(new_permits)

# ── Pending permits scraper ─────────────────────────────────────────────────────
def scrape_pending_page(page=0):
    """
    Fetch one HTML page of pending permit applications (~8 pages, ~320 total).
    Columns: Facility+AppNum (combined), Site ID, City/County, Permit Type,
             Application Number, Date Administratively Complete
    Returns list of dicts, None on network error, [] when past last page.
    """
    url = _build_url(PENDING_HTML)
    try:
        r = requests.get(url, params={'page': page, 'order': 'field_date_issued', 'sort': 'desc'},
                         headers=SCRAPE_HEADERS, timeout=30)
        r.raise_for_status()
    except Exception as e:
        logger.error(f"Pending page {page} failed: {e}")
        return None

    soup = BeautifulSoup(r.text, 'html.parser')
    rows = soup.select('table tbody tr')
    if not rows:
        return []

    results = []
    for row in rows:
        cells = row.select('td')
        if len(cells) < 6:
            continue

        app_link    = cells[4].select_one('a')
        app_num     = app_link.get_text(strip=True) if app_link else cells[4].get_text(strip=True)
        dnr_path    = app_link['href'] if app_link else ''
        dnr_url     = f"{DNR_BASE}{dnr_path}" if dnr_path else ''

        # Cell 0 is "Facility Name, AppNumber" — strip the app number off the end
        full_text   = cells[0].get_text(strip=True)
        facility    = full_text.replace(f', {app_num}', '').strip() if app_num else full_text

        site_id     = cells[1].get_text(strip=True)
        loc_text    = cells[2].get_text(separator=',', strip=True)
        loc_parts   = [p.strip() for p in loc_text.split(',') if p.strip()]
        city        = loc_parts[0] if len(loc_parts) >= 2 else ''
        county      = loc_parts[1] if len(loc_parts) >= 2 else (loc_parts[0] if loc_parts else '')
        permit_type = cells[3].get_text(strip=True)
        date_str    = cells[5].get_text(strip=True)

        date_ts = None
        try:
            date_ts = datetime.strptime(date_str, '%m/%d/%Y')
        except Exception:
            pass

        if app_num:
            results.append({
                'application_number': app_num,
                'facility_name':      facility,
                'site_id':            site_id,
                'city':               city,
                'county':             county,
                'permit_type':        permit_type,
                'date_complete':      date_str,
                'date_complete_ts':   date_ts,
                'dnr_url':            dnr_url,
            })
    return results

def sync_pending():
    """Scrape all pending permit applications. Returns count of new records."""
    logger.info("Pending permit sync started")
    conn = get_db()
    c    = conn.cursor()
    new_pending = []
    page  = 0
    errors = 0

    while True:
        rows = scrape_pending_page(page=page)

        if rows is None:
            errors += 1
            if errors >= 5:
                logger.error("Too many errors on pending sync, aborting")
                break
            time.sleep(5)
            continue

        if rows == []:
            break

        for p in rows:
            c.execute('SELECT id FROM pending_permits WHERE application_number = ?',
                      (p['application_number'],))
            if c.fetchone():
                continue
            c.execute('''
                INSERT OR IGNORE INTO pending_permits
                    (application_number, facility_name, site_id, city, county,
                     permit_type, date_complete, date_complete_ts, dnr_url)
                VALUES (:application_number,:facility_name,:site_id,:city,:county,
                        :permit_type,:date_complete,:date_complete_ts,:dnr_url)
            ''', p)
            if c.lastrowid:
                new_pending.append({**p, 'id': c.lastrowid})

        conn.commit()
        logger.info(f"Pending page {page}: total new = {len(new_pending)}")
        page += 1
        time.sleep(0.5)

    conn.close()
    logger.info(f"Pending sync complete: {len(new_pending)} new applications")
    if new_pending:
        notify_subscribers_pending(new_pending)
    return len(new_pending)

def sync_all():
    """Run both issued and pending syncs. Returns dict of counts."""
    issued  = sync_permits()
    pending = sync_pending()
    return {'issued': issued, 'pending': pending}

# ── Email ───────────────────────────────────────────────────────────────────────
def _smtp_send(to_email, subject, html_body):
    if not SMTP_USER or not SMTP_PASS:
        logger.warning("SMTP not configured; skipping email")
        return
    msg = MIMEMultipart('alternative')
    msg['Subject'] = subject
    msg['From']    = SMTP_USER
    msg['To']      = to_email
    msg.attach(MIMEText(html_body, 'html'))
    try:
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT) as s:
            s.login(SMTP_USER, SMTP_PASS)
            s.sendmail(SMTP_USER, to_email, msg.as_string())
        logger.info(f"Email sent → {to_email}")
    except Exception as e:
        logger.error(f"Email failed → {to_email}: {e}")

def send_verification_email(to_email, token):
    verify_url = f"{BASE_URL}/verify/{token}"
    html = f"""
    <html><body style="font-family:Arial,sans-serif;max-width:600px;margin:40px auto;">
      <h2 style="color:#1a5276;">Confirm your MO Permits subscription</h2>
      <p>Click the button below to confirm your email and start receiving air quality permit alerts.</p>
      <p>
        <a href="{verify_url}"
           style="display:inline-block;background:#1a5276;color:white;padding:12px 24px;
                  text-decoration:none;border-radius:4px;font-weight:bold;">
          Verify Email
        </a>
      </p>
      <p style="color:#666;font-size:13px;">Or copy this link: {verify_url}</p>
      <p style="color:#999;font-size:12px;">If you didn't request this, you can safely ignore this email.</p>
    </body></html>"""
    _smtp_send(to_email, "Verify your MO Permits subscription", html)

def notify_subscribers(new_permits):
    conn = get_db()
    c    = conn.cursor()
    c.execute('SELECT * FROM subscribers WHERE verified = 1')
    subscribers = [dict(r) for r in c.fetchall()]
    conn.close()
    if not subscribers:
        return
    for sub in subscribers:
        county_filter = sub.get('county_filter', 'all')
        type_filter   = sub.get('permit_type_filter', 'all')
        matching = []
        for p in new_permits:
            c_match = county_filter == 'all' or any(
                p['county'].lower() == cf.strip().lower()
                for cf in county_filter.split(','))
            t_match = type_filter == 'all' or any(
                tf.strip().lower() in p['permit_type'].lower()
                for tf in type_filter.split(','))
            if c_match and t_match:
                matching.append(p)
        if matching:
            _send_permit_digest(sub['email'], matching, sub['unsubscribe_token'])

def _send_permit_digest(to_email, permits, unsub_token):
    count      = len(permits)
    unsub_url  = f"{BASE_URL}/unsubscribe/{unsub_token}"
    subject    = f"MO DNR: {count} New Air Quality Permit{'s' if count > 1 else ''} Issued"
    rows_html  = ''
    for p in permits:
        detail_url = f"{BASE_URL}/permits/{_permit_url_slug(p['permit_number'])}"
        rows_html += f"""
        <tr>
          <td style="padding:8px 12px;border-bottom:1px solid #eee;">
            <a href="{detail_url}" style="color:#1a5276;text-decoration:none;">{p['facility_name']}</a>
          </td>
          <td style="padding:8px 12px;border-bottom:1px solid #eee;font-family:monospace;">{p['permit_number']}</td>
          <td style="padding:8px 12px;border-bottom:1px solid #eee;">{p['city']}{', ' if p['city'] else ''}{p['county']}</td>
          <td style="padding:8px 12px;border-bottom:1px solid #eee;">{p['permit_type']}</td>
          <td style="padding:8px 12px;border-bottom:1px solid #eee;white-space:nowrap;">{p['date_issued']}</td>
        </tr>"""
    html = f"""
    <html><body style="font-family:Arial,sans-serif;max-width:900px;margin:40px auto;">
      <h2 style="color:#1a5276;margin-bottom:4px;">MO DNR Air Quality Permit Alert</h2>
      <p style="color:#555;">{count} new permit{'s have' if count>1 else ' has'} been issued.</p>
      <table style="width:100%;border-collapse:collapse;margin:16px 0;">
        <thead>
          <tr style="background:#1a5276;color:white;text-align:left;">
            <th style="padding:10px 12px;">Facility</th>
            <th style="padding:10px 12px;">Permit #</th>
            <th style="padding:10px 12px;">Location</th>
            <th style="padding:10px 12px;">Type</th>
            <th style="padding:10px 12px;">Date Issued</th>
          </tr>
        </thead>
        <tbody>{rows_html}</tbody>
      </table>
      <p style="font-size:13px;color:#888;margin-top:24px;">
        <a href="{BASE_URL}" style="color:#1a5276;">View all permits</a> &nbsp;|&nbsp;
        <a href="{unsub_url}" style="color:#999;">Unsubscribe</a>
      </p>
    </body></html>"""
    _smtp_send(to_email, subject, html)

def notify_subscribers_pending(new_pending):
    conn = get_db()
    c    = conn.cursor()
    c.execute('SELECT * FROM subscribers WHERE verified = 1')
    subscribers = [dict(r) for r in c.fetchall()]
    conn.close()
    if not subscribers:
        return
    for sub in subscribers:
        county_filter = sub.get('county_filter', 'all')
        type_filter   = sub.get('permit_type_filter', 'all')
        matching = []
        for p in new_pending:
            c_match = county_filter == 'all' or any(
                p['county'].lower() == cf.strip().lower()
                for cf in county_filter.split(','))
            t_match = type_filter == 'all' or any(
                tf.strip().lower() in p['permit_type'].lower()
                for tf in type_filter.split(','))
            if c_match and t_match:
                matching.append(p)
        if matching:
            _send_pending_digest(sub['email'], matching, sub['unsubscribe_token'])

def _send_pending_digest(to_email, applications, unsub_token):
    count     = len(applications)
    unsub_url = f"{BASE_URL}/unsubscribe/{unsub_token}"
    subject   = f"MO DNR: {count} New Air Quality Permit Application{'s' if count > 1 else ''}"
    rows_html = ''
    for p in applications:
        rows_html += f"""
        <tr>
          <td style="padding:8px 12px;border-bottom:1px solid #eee;">{p['facility_name']}</td>
          <td style="padding:8px 12px;border-bottom:1px solid #eee;font-family:monospace;">{p['application_number']}</td>
          <td style="padding:8px 12px;border-bottom:1px solid #eee;">{p['city']}{', ' if p['city'] else ''}{p['county']}</td>
          <td style="padding:8px 12px;border-bottom:1px solid #eee;">{p['permit_type']}</td>
          <td style="padding:8px 12px;border-bottom:1px solid #eee;white-space:nowrap;">{p['date_complete']}</td>
        </tr>"""
    html = f"""
    <html><body style="font-family:Arial,sans-serif;max-width:900px;margin:40px auto;">
      <h2 style="color:#7d6608;margin-bottom:4px;">MO DNR Air Quality Permit Application Alert</h2>
      <p style="color:#555;">{count} new permit application{'s have' if count>1 else ' has'} been filed — not yet issued.</p>
      <table style="width:100%;border-collapse:collapse;margin:16px 0;">
        <thead>
          <tr style="background:#7d6608;color:white;text-align:left;">
            <th style="padding:10px 12px;">Facility</th>
            <th style="padding:10px 12px;">Application #</th>
            <th style="padding:10px 12px;">Location</th>
            <th style="padding:10px 12px;">Type</th>
            <th style="padding:10px 12px;">Date Complete</th>
          </tr>
        </thead>
        <tbody>{rows_html}</tbody>
      </table>
      <p style="font-size:13px;color:#888;margin-top:24px;">
        <a href="{BASE_URL}/pending" style="color:#7d6608;">View all pending applications</a> &nbsp;|&nbsp;
        <a href="{unsub_url}" style="color:#999;">Unsubscribe</a>
      </p>
    </body></html>"""
    _smtp_send(to_email, subject, html)

# ── Helpers ─────────────────────────────────────────────────────────────────────
def _permit_url_slug(permit_number):
    """Make permit number URL-safe."""
    return permit_number.replace('/', '--')

def _permit_from_slug(slug):
    """Reverse URL-safe slug back to permit number."""
    return slug.replace('--', '/')

# ── Routes ──────────────────────────────────────────────────────────────────────
@app.route('/')
def index():
    conn = get_db()
    c    = conn.cursor()
    c.execute('''
        SELECT * FROM permits
        ORDER BY date_issued_ts DESC, first_seen DESC
        LIMIT 50
    ''')
    recent = [dict(r) for r in c.fetchall()]
    c.execute('SELECT COUNT(*) FROM permits')
    total = c.fetchone()[0]
    c.execute("SELECT MAX(first_seen) FROM permits")
    last_sync = c.fetchone()[0]
    c.execute('''
        SELECT date_issued_ts, COUNT(*) as cnt
        FROM permits
        WHERE date_issued_ts IS NOT NULL
        GROUP BY strftime('%Y-%m', date_issued_ts)
        ORDER BY date_issued_ts DESC
        LIMIT 12
    ''')
    monthly = [dict(r) for r in c.fetchall()]
    conn.close()
    return render_template('index.html', permits=recent, total=total,
                           last_sync=last_sync, monthly=monthly)

@app.route('/permits/<slug>')
def permit_detail(slug):
    permit_number = _permit_from_slug(slug)
    conn = get_db()
    c    = conn.cursor()
    c.execute('SELECT * FROM permits WHERE permit_number = ?', (permit_number,))
    row = c.fetchone()
    conn.close()
    if not row:
        abort(404)
    return render_template('permit.html', permit=dict(row))

@app.route('/search')
def search():
    q           = request.args.get('q', '').strip()
    county      = request.args.get('county', '').strip()
    ptype       = request.args.get('type', '').strip()
    page        = max(1, int(request.args.get('page', 1)))
    per_page    = 25

    conn   = get_db()
    c      = conn.cursor()
    where  = ['1=1']
    params = []

    if q:
        where.append('(facility_name LIKE ? OR permit_number LIKE ? OR site_id LIKE ?)')
        params += [f'%{q}%', f'%{q}%', f'%{q}%']
    if county:
        where.append('county LIKE ?')
        params.append(f'%{county}%')
    if ptype:
        where.append('permit_type LIKE ?')
        params.append(f'%{ptype}%')

    sql_base = f"FROM permits WHERE {' AND '.join(where)}"
    c.execute(f"SELECT COUNT(*) {sql_base}", params)
    total = c.fetchone()[0]

    c.execute(
        f"SELECT * {sql_base} ORDER BY date_issued_ts DESC LIMIT ? OFFSET ?",
        params + [per_page, (page - 1) * per_page]
    )
    permits     = [dict(r) for r in c.fetchall()]
    total_pages = max(1, (total + per_page - 1) // per_page)
    conn.close()

    return render_template('search.html',
        permits=permits, q=q, county=county, ptype=ptype,
        page=page, total=total, total_pages=total_pages,
        counties=MO_COUNTIES, permit_type_groups=PERMIT_TYPE_GROUPS)

@app.route('/subscribe', methods=['GET', 'POST'])
def subscribe():
    if request.method == 'POST':
        email         = request.form.get('email', '').strip().lower()
        county_filter = ','.join(request.form.getlist('county_filter')) or 'all'
        type_filter   = ','.join(request.form.getlist('type_filter'))   or 'all'

        if not email or '@' not in email:
            return render_template('subscribe.html',
                error='Please enter a valid email address.',
                counties=MO_COUNTIES, permit_type_groups=PERMIT_TYPE_GROUPS)

        conn = get_db()
        c    = conn.cursor()
        c.execute('SELECT id, verified FROM subscribers WHERE email = ?', (email,))
        existing = c.fetchone()

        if existing and existing['verified']:
            conn.close()
            return render_template('subscribe.html',
                error='This email is already subscribed.',
                counties=MO_COUNTIES, permit_type_groups=PERMIT_TYPE_GROUPS)

        verify_token = secrets.token_urlsafe(32)
        unsub_token  = secrets.token_urlsafe(32)

        if existing:
            c.execute(
                'UPDATE subscribers SET verify_token=?, county_filter=?, permit_type_filter=? WHERE email=?',
                (verify_token, county_filter, type_filter, email))
        else:
            c.execute('''
                INSERT INTO subscribers (email, county_filter, permit_type_filter, verify_token, unsubscribe_token)
                VALUES (?,?,?,?,?)
            ''', (email, county_filter, type_filter, verify_token, unsub_token))

        conn.commit()
        conn.close()
        send_verification_email(email, verify_token)
        return render_template('subscribe.html', success=True,
            counties=MO_COUNTIES, permit_type_groups=PERMIT_TYPE_GROUPS)

    return render_template('subscribe.html',
        counties=MO_COUNTIES, permit_type_groups=PERMIT_TYPE_GROUPS)

@app.route('/verify/<token>')
def verify(token):
    conn = get_db()
    c    = conn.cursor()
    c.execute('SELECT id FROM subscribers WHERE verify_token = ?', (token,))
    row = c.fetchone()
    if row:
        c.execute('UPDATE subscribers SET verified=1, verify_token=NULL WHERE id=?', (row['id'],))
        conn.commit()
        conn.close()
        return render_template('verified.html')
    conn.close()
    return render_template('verified.html', error=True)

@app.route('/unsubscribe/<token>')
def unsubscribe(token):
    conn = get_db()
    c    = conn.cursor()
    c.execute('SELECT id FROM subscribers WHERE unsubscribe_token = ?', (token,))
    row = c.fetchone()
    if row:
        c.execute('DELETE FROM subscribers WHERE id=?', (row['id'],))
        conn.commit()
        conn.close()
        return render_template('unsubscribed.html')
    conn.close()
    return render_template('unsubscribed.html', error=True)

@app.route('/stats')
def stats():
    conn = get_db()
    c    = conn.cursor()
    c.execute('SELECT COUNT(*) FROM permits')
    total = c.fetchone()[0]
    c.execute('''SELECT permit_type, COUNT(*) cnt FROM permits
                 GROUP BY permit_type ORDER BY cnt DESC''')
    by_type = [dict(r) for r in c.fetchall()]
    c.execute('''SELECT county, COUNT(*) cnt FROM permits
                 WHERE county != ""
                 GROUP BY county ORDER BY cnt DESC LIMIT 25''')
    by_county = [dict(r) for r in c.fetchall()]
    c.execute("SELECT COUNT(*) FROM subscribers WHERE verified=1")
    subs = c.fetchone()[0]
    c.execute('''SELECT strftime('%Y', date_issued_ts) yr, COUNT(*) cnt
                 FROM permits WHERE date_issued_ts IS NOT NULL
                 GROUP BY yr ORDER BY yr DESC''')
    by_year = [dict(r) for r in c.fetchall()]
    conn.close()
    return render_template('stats.html', total=total, by_type=by_type,
                           by_county=by_county, subscribers=subs, by_year=by_year)

@app.route('/pending')
def pending():
    conn = get_db()
    c    = conn.cursor()
    q      = request.args.get('q', '').strip()
    county = request.args.get('county', '').strip()
    ptype  = request.args.get('type', '').strip()
    page   = max(1, int(request.args.get('page', 1)))
    per_page = 25

    where  = ['1=1']
    params = []
    if q:
        where.append('(facility_name LIKE ? OR application_number LIKE ? OR site_id LIKE ?)')
        params += [f'%{q}%', f'%{q}%', f'%{q}%']
    if county:
        where.append('county LIKE ?')
        params.append(f'%{county}%')
    if ptype:
        where.append('permit_type LIKE ?')
        params.append(f'%{ptype}%')

    sql_base = f"FROM pending_permits WHERE {' AND '.join(where)}"
    c.execute(f"SELECT COUNT(*) {sql_base}", params)
    total = c.fetchone()[0]
    c.execute(
        f"SELECT * {sql_base} ORDER BY date_complete_ts DESC LIMIT ? OFFSET ?",
        params + [per_page, (page - 1) * per_page]
    )
    applications = [dict(r) for r in c.fetchall()]
    total_pages  = max(1, (total + per_page - 1) // per_page)
    conn.close()

    return render_template('pending.html',
        applications=applications, q=q, county=county, ptype=ptype,
        page=page, total=total, total_pages=total_pages,
        counties=MO_COUNTIES, permit_type_groups=PERMIT_TYPE_GROUPS)

@app.route('/api/sync', methods=['POST'])
def api_sync():
    """Trigger a manual sync of both issued and pending permits."""
    results = sync_all()
    return jsonify({'status': 'ok', **results})

@app.route('/api/sync/issued', methods=['POST'])
def api_sync_issued():
    return jsonify({'status': 'ok', 'issued': sync_permits()})

@app.route('/api/sync/pending', methods=['POST'])
def api_sync_pending():
    return jsonify({'status': 'ok', 'pending': sync_pending()})

@app.route('/health')
def health():
    return jsonify({'status': 'ok'})

# ── Template filters ─────────────────────────────────────────────────────────────
@app.template_filter('permit_slug')
def permit_slug_filter(permit_number):
    return _permit_url_slug(permit_number)

@app.template_filter('permit_badge')
def permit_badge_filter(permit_type):
    from markupsafe import Markup
    pt = (permit_type or '').lower()
    if 'part 70' in pt:
        css = 'badge-part70'
    elif 'intermediate' in pt:
        css = 'badge-intermediate'
    elif 'construction' in pt:
        css = 'badge-construction'
    elif 'operating' in pt:
        css = 'badge-operating'
    elif 'general' in pt or 'rule' in pt:
        css = 'badge-general'
    else:
        css = 'badge-other'
    return Markup(f'<span class="badge {css}">{permit_type}</span>')

# ── Scheduler ────────────────────────────────────────────────────────────────────
def run_scheduler():
    schedule.every().day.at("06:00").do(sync_all)
    schedule.every().day.at("14:00").do(sync_all)
    logger.info("Scheduler running (issued + pending sync at 06:00 and 14:00 daily)")
    while True:
        schedule.run_pending()
        time.sleep(60)

# ── Boot ─────────────────────────────────────────────────────────────────────────
init_db()
threading.Thread(target=run_scheduler, daemon=True).start()

if __name__ == '__main__':
    app.run(debug=True, port=8000)
