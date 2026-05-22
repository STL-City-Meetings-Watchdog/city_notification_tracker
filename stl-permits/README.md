# stl-permits

Daily digest + public archive of City of St. Louis building permits.
Companion to [stl-meetings](https://stlmeetings.veiledprofits.com) — same architecture, same VPS, same Cloudflare Worker proxy.

## What it does

- Pulls the city's 30-day issued-permits CSV every 6 hours through the existing Cloudflare Worker (`stl-proxy`).
- Stores everything in SQLite. Dedupes on `SHA1(address | app_date | desc | cost)` since the feed doesn't expose a permit number.
- Serves a public web UI at `permits.veiledprofits.com`:
  - `/` — latest 50 permits
  - `/search?q=&type=&min_cost=` — search by address, description, type, minimum cost
  - `/permit/<id>` — detail view
  - `/subscribe`, `/verify/<token>`, `/unsubscribe/<token>` — double-opt-in email list
  - `/api/permits?limit=N` — JSON
  - `/health` — JSON health & last-sync status
- Sends a daily HTML digest at configurable local hour to verified subscribers.
- **Initial seed** (first sync) marks everything as already-notified so the first subscriber doesn't get blasted with 30 days of history.

## Data source

- CSV endpoint: `https://www.stlouis-mo.gov/customcf/endpoints/building-permits/building-permits-30-days-export.cfm?permitType=all&dataType=csv`
- 8 columns: ADDRESS, APPLICATIONDATE, APPLICATIONDESCRIPTION, DAYSTOISSUE, ESTPROJECTCOST, ISSUEDATE, PROJECTTYPE, STRUCTURETYPE.
- Feed contains only **issued** permits (pending applications are not included).
- Volume: ~12 permits/day on average.

## Prerequisites on the VPS

Same VPS as stl-meetings (Hostinger KVM 4, `veil`). Assumes:

- Docker + docker-compose
- Nginx Proxy Manager running on `npm_default` Docker network (port 80/443)
- Cloudflare Worker `stl-proxy.dan-f8a.workers.dev` (shared with stl-meetings, no changes needed — it already whitelists `stlouis-mo.gov`)

## Pre-deploy tasks (user)

1. **Create mailbox** `permits@veiledprofits.com` in Hostinger email admin (or add as alias to existing box if mailbox count is at limit).
2. **DNS**: `permits.veiledprofits.com` A-record → ``veil``.
3. **NPM proxy host** (in Nginx Proxy Manager):
   - Domain: `permits.veiledprofits.com`
   - Scheme: `http`
   - Forward Hostname: `stl-permits-app-1`
   - Forward Port: `8000`
   - SSL: Let's Encrypt, Force SSL on.

## Deploy

```sh
# 1. Copy project to VPS
rsync -avz --exclude .env --exclude data /path/to/stl-permits/ root@veil:/root/stl-permits/

# 2. SSH in
ssh root@veil

# 3. Create .env
cd /root/stl-permits
cp .env.example .env
$EDITOR .env   # fill SMTP_PASS, confirm other values

# 4. Build & start
docker compose up -d --build

# 5. Attach container to NPM network (automatic via compose, but confirm)
docker network connect npm_default stl-permits-app-1 2>/dev/null || true

# 6. Verify
docker logs stl-permits-app-1 --tail 50
curl -s http://localhost:8002/health | jq
```

First sync should run automatically on container start (seed mode, no emails sent). After that, sync every `SYNC_INTERVAL_HOURS` (default 6h) and digest daily at `DIGEST_HOUR` (default 07:00 server time).

## Useful commands

```sh
# Force a sync now
docker exec -it stl-permits-app-1 python -c "from main import sync_permits; print(sync_permits(True))"

# Count permits in DB
docker exec -it stl-permits-app-1 python -c "
import sqlite3
c = sqlite3.connect('/app/data/permits.db')
print('permits:', c.execute('SELECT COUNT(*) FROM permits').fetchone()[0])
print('subs:',    c.execute('SELECT COUNT(*) FROM subscribers WHERE verified=1').fetchone()[0])
print('last sync:', c.execute('SELECT * FROM sync_log ORDER BY id DESC LIMIT 1').fetchone())
"

# Force a digest (even if nothing is unnotified — use for testing)
docker exec -it stl-permits-app-1 python -c "from main import send_digest; send_digest()"

# Send test verify email to yourself (adjust the email)
docker exec -it stl-permits-app-1 python -c "
from main import get_db_standalone, send_verify_email
import secrets
c = get_db_standalone()
c.execute('INSERT OR IGNORE INTO subscribers (email, verify_token, unsubscribe_token) VALUES (?, ?, ?)',
          ('dan@danielpate.com', secrets.token_urlsafe(24), secrets.token_urlsafe(24)))
c.commit()
sub = c.execute('SELECT * FROM subscribers WHERE email = ?', ('dan@danielpate.com',)).fetchone()
send_verify_email(sub)
"

# Rebuild after code change
cd /root/stl-permits && docker compose up -d --build
```

## Config (.env)

| Key | Default | Notes |
|---|---|---|
| `SMTP_HOST` | `smtp.hostinger.com` | Shared with stl-meetings SMTP |
| `SMTP_PORT` | `465` | SSL |
| `SMTP_USER` | `permits@veiledprofits.com` | **Create this mailbox first** |
| `SMTP_PASS` | — | |
| `BASE_URL` | `https://permits.veiledprofits.com` | Used in emails |
| `PROXY_BASE` | `https://stl-proxy.dan-f8a.workers.dev/?url=` | Reused CF Worker |
| `DIGEST_HOUR` | `7` | 24h, server local time |
| `SYNC_INTERVAL_HOURS` | `6` | Pull cadence |

## File layout

```
stl-permits/
├── docker-compose.yml
├── .env.example
├── .env                  # (you create, not in repo)
├── README.md
├── data/                 # (created by container; SQLite lives here)
│   └── permits.db
└── app/
    ├── Dockerfile
    ├── requirements.txt
    ├── main.py           # Flask app + scheduler + sync/digest logic
    └── templates/
        ├── base.html
        ├── index.html
        ├── permit.html
        ├── search.html
        ├── subscribe.html
        ├── verified.html
        ├── unsubscribed.html
        ├── email_verify.html
        └── email_digest.html
```

## Known limitations (intentional, Phase 1)

- No filtering on subscribe (firehose digest to start).
- No keyword watchlist (planned for Phase 2).
- No ward/neighborhood enrichment.
- Only issued permits (pending-application feed not yet located).
- No MODNR air/water permits yet (Phase 2).
- `STRUCTURETYPE` codes are stored raw; no human-readable decoder yet.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| 502 Bad Gateway at the domain | Container not on `npm_default` network | `docker network connect npm_default stl-permits-app-1` |
| `Fetch failed` in logs | Cloudflare Worker down or URL encoding issue | Check https://stl-proxy.dan-f8a.workers.dev/?url=... manually |
| No rows appearing | City feed format changed | Inspect `data/permits.db` sync_log table for error text |
| Digest never arrives | All rows inserted during seed sync are `notified=1` | Expected on day 1; next sync picks up new rows and digest fires the next morning |
| SMTP auth failure | Wrong password or mailbox not created yet | Verify in Hostinger email admin; test via `swaks` if available |
