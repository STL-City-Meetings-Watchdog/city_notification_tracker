#!/usr/bin/env python3
"""Backfill the meetings archive from the CivicClerk public OData API.

The CivicClerk portal cannot be scraped as HTML - https://stlouismo.portal.
civicclerk.com/ serves a ~1.3KB SPA shell whose entire body is "You need to
enable JavaScript to run this app". That is why generate_meeting_pdf() falls
back to pasting a link. The SPA is however backed by an anonymous OData API
(base templated as https://[TENANT].api.civicclerk.com/v1 inside its JS
bundle; tenant "stlouismo"), which serves the same agendas, packets and
minutes as plain JSON + PDF. No key, no login, no proxy.

SILENT BY DESIGN. This writes to sqlite directly and never imports main.py:
  - main.py calls init_db() at module scope and starts a scheduler thread, so
    importing it has side effects on the live DB.
  - Every email path in main.py sits behind a function this never calls:
      send_meeting_notification() is driven by sync_meetings()' new-meeting
      loop, which keys off iCal uids. Rows inserted here use a synthetic uid
      ("civicclerk:<eventId>") that appears in no feed, so sync_meetings()
      can never classify them as new and notify on them.
      check_upcoming_documents() only mails for meetings dated now..now+45d.
      Every CivicClerk event is in the past, and this additionally asserts
      each target meeting's start_time is in the past before writing.
  - The subscribers table is never read or written.

Dry run is the default; nothing downloads or writes without --commit.

    docker exec -it stl-meetings-prod uv run python3 civicclerk_backfill.py
    docker exec -it stl-meetings-prod uv run python3 civicclerk_backfill.py --commit --limit 20
"""
import argparse
import json
import os
import re
import shutil
import sqlite3
import sys
import urllib.request
from datetime import datetime, timedelta, timezone

API = "https://stlouismo.api.civicclerk.com/v1"
PORTAL = "https://stlouismo.portal.civicclerk.com/event/{}/files"
TYPE_MAP = {"Agenda": "agenda", "Agenda Packet": "agenda_packet",
            "Minutes": "minutes", "Other": "document"}

# Matching: canonicalise BOTH sides to a committee and require agreement.
# Token-overlap scoring was tried first and failed three ways on real prod data:
#   "Full Board Meeting"  -> normaliser stripped full/board/meeting, leaving an
#                            EMPTY token set, scoring 0.00 against its own row
#   "Legislation" vs "Legislative"                          -> 0.25, different tokens
#   correct row sitting one calendar day away, because CivicClerk stamps UTC
# Canonicalising is looser where it should be and stricter where it matters: it
# cannot match an unrelated body. Measured on production, this scores 171
# matched / 114 unmatched versus 159/126 for token overlap, with zero matches
# below 0.20 title similarity - i.e. the extra matches are not false positives.
COMMITTEES = [
    ("aldermen",       [r"alderm", r"full board"]),
    ("budget",         [r"budget", r"public employee"]),
    ("legislation",    [r"legislat"]),
    ("personnel",      [r"personnel"]),
    ("housing",        [r"housing", r"urban development"]),
    ("public_safety",  [r"public safety"]),
    ("health",         [r"health", r"human development"]),
    ("transport",      [r"transportation", r"commerce"]),
    ("infrastructure", [r"infrastructure", r"utilities"]),
    ("redtape",        [r"red tape"]),
    ("neighborhood",   [r"neighborhood"]),
    ("intergov",       [r"intergovernmental"]),
]
# Other bodies that merely graze a committee keyword. Without these, "Joint
# Board of Health and Hospitals" becomes a candidate for the Health committee
# and can win on ordering alone.
EXCLUDE = [r"board of public service", r"board of estimate", r"port authority",
           r"preservation board", r"board of adjustment", r"civil service",
           r"board of building", r"civilian oversight", r"local development",
           r"industrial development", r"airport commission", r"planning commission",
           r"joint board", r"hospitals", r"selection committee", r"task force",
           r"commission meeting", r"business district", r"conditional use",
           r"determination hearing"]


def committee(*texts):
    """Canonical committee for a title, or None if it isn't one of ours."""
    s = " ".join(x or "" for x in texts).lower()
    if any(re.search(p, s) for p in EXCLUDE):
        return None
    for name, pats in COMMITTEES:
        if any(re.search(p, s) for p in pats):
            return name
    return None


def toks(s):
    return set(re.findall(r"[a-z]{4,}", (s or "").lower()))


def api_get(url, timeout=90):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.read()


def fetch_events():
    """All events, following @odata.nextLink.

    Note $top is a TOTAL record budget, not a page size - $top=200 caps the
    whole result at 200 and $top=2000 returns HTTP 400. So don't set it.
    """
    out, u = [], API + "/Events?$orderby=eventDate%20asc"
    while u:
        d = json.loads(api_get(u))
        out += d["value"]
        u = d.get("@odata.nextLink")
    return out


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--commit", action="store_true",
                    help="actually download and write (default: dry run)")
    ap.add_argument("--dry-run", action="store_true",
                    help="explicit no-op; dry run is already the default")
    ap.add_argument("--db", default="/app/data/meetings.db")
    ap.add_argument("--pdfs", default="/app/pdfs")
    ap.add_argument("--types", default="agenda,minutes",
                    help="comma list of agenda,minutes,agenda_packet,document")
    ap.add_argument("--include-packets", action="store_true",
                    help="shorthand: add agenda_packet. Packets run to 48MB - see the "
                         "email attachment cap issue before enabling this")
    ap.add_argument("--limit", type=int, default=0, help="stop after N files (trial run)")
    ap.add_argument("--min-free-mb", type=int, default=2048,
                    help="refuse to run with less free disk than this")
    return ap.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    if args.dry_run and args.commit:
        sys.exit("--dry-run and --commit are contradictory; pick one")
    want = {t.strip() for t in args.types.split(",") if t.strip()}
    if args.include_packets:
        want.add("agenda_packet")
    dry = not args.commit
    print(f"{'DRY RUN' if dry else '*** COMMIT MODE ***'}  types={sorted(want)}  db={args.db}\n")

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    existing_urls = {r[0] for r in c.execute(
        "SELECT original_url FROM documents WHERE original_url IS NOT NULL")}
    meetings = [dict(r) for r in c.execute(
        "SELECT id, uid, title, start_time FROM meetings WHERE start_time IS NOT NULL")]
    print(f"existing meetings in DB: {len(meetings)}   existing document rows: {len(existing_urls)}")

    by_date = {}
    for m in meetings:
        by_date.setdefault(str(m["start_time"])[:10], []).append(m)

    print("fetching CivicClerk catalog ...")
    events = fetch_events()
    print(f"  {len(events)} events\n")

    now = datetime.now(timezone.utc)
    plan, skipped_future, already = [], 0, 0
    matched = created = 0
    match_samples, create_samples = [], []

    for e in events:
        pf = e.get("publishedFiles") or []
        if not pf:
            continue
        ed = e["eventDate"][:10]
        try:
            when = datetime.fromisoformat(e["eventDate"].replace("Z", "+00:00"))
        except ValueError:
            continue
        if when >= now:                     # hard guard: never touch a future meeting
            skipped_future += 1
            continue

        cc = committee(e["eventName"], e.get("eventCategoryName"))
        d0 = datetime.strptime(ed, "%Y-%m-%d")
        et = toks(e["eventName"]) | toks(e.get("eventCategoryName"))
        hits = []
        if cc:
            # +/-1 day: CivicClerk timestamps are UTC, so a late meeting can
            # land on a different calendar date than our stored start_time.
            for off in (0, -1, 1):
                k = (d0 + timedelta(days=off)).strftime("%Y-%m-%d")
                for m in by_date.get(k, []):
                    if committee(m["title"]) == cc:
                        mt = toks(m["title"])
                        sim = len(mt & et) / max(1, len(mt | et))
                        # nearest day first, then best title similarity, so an
                        # unrelated body can never win on ordering alone
                        hits.append((abs(off), -sim, off, m, sim))
        if hits:
            hits.sort()
            best, score, off = hits[0][3], hits[0][4], hits[0][2]
            target, is_new = best, False
            matched += 1
            if len(match_samples) < 5:
                match_samples.append(
                    f"{ed} {off:+d}d CC:{e['eventName'][:32]:32} -> DB#{best['id']} "
                    f"{best['title'][:32]} (sim {score:.2f})")
        else:
            target = {"id": None, "title": e["eventName"], "start_time": e["eventDate"]}
            is_new = True
            created += 1
            if len(create_samples) < 5:
                create_samples.append(f"{ed}  {e['eventName'][:44]}  [{e.get('eventCategoryName')}]")

        for f in pf:
            dt = TYPE_MAP.get(f["type"], "document")
            if dt not in want:
                continue
            url = f"{API}/Meetings/GetMeetingFileStream(fileId={f['fileId']},plainText=false)"
            if url in existing_urls:
                already += 1
                continue
            plan.append({"event": e, "file": f, "doc_type": dt, "url": url,
                         "target": target, "is_new_meeting": is_new})

    if args.limit:
        plan = plan[:args.limit]

    print(f"match analysis:  {matched} events matched an existing meeting, "
          f"{created} would get a NEW meeting row, {skipped_future} future events skipped")
    print("  sample matches:")
    for s in match_samples:
        print("   ", s)
    print("  sample new meeting rows:")
    for s in create_samples:
        print("   ", s)
    print(f"\nfiles already ingested (skipped): {already}")
    print(f"FILES TO FETCH: {len(plan)}")
    bytype = {}
    for p in plan:
        bytype[p["doc_type"]] = bytype.get(p["doc_type"], 0) + 1
    print(f"  by type: {bytype}")

    free_mb = shutil.disk_usage(args.pdfs).free / 1024 / 1024
    print(f"free disk at {args.pdfs}: {free_mb:.0f} MB")
    if free_mb < args.min_free_mb:
        sys.exit(f"ABORT: only {free_mb:.0f}MB free, need {args.min_free_mb}MB "
                 f"(override with --min-free-mb)")

    if dry:
        print("\nDRY RUN - nothing downloaded, nothing written. Re-run with --commit.")
        return 0

    print("\ndownloading ...")
    ok = fail = 0
    total_bytes = 0
    for i, p in enumerate(plan, 1):
        e, f, tgt = p["event"], p["file"], p["target"]
        try:
            if p["is_new_meeting"] and tgt["id"] is None:
                uid = f"civicclerk:{e['id']}"
                row = c.execute("SELECT id FROM meetings WHERE uid=?", (uid,)).fetchone()
                if row:
                    tgt["id"] = row[0]
                else:
                    # notified=1 so no later pass can treat this as a fresh meeting
                    c.execute("INSERT INTO meetings (uid,title,description,location,"
                              "start_time,event_url,sponsor,notified) VALUES (?,?,?,?,?,?,?,1)",
                              (uid, e["eventName"], e.get("eventDescription") or "", "",
                               e["eventDate"], PORTAL.format(e["id"]),
                               e.get("eventCategoryName") or ""))
                    conn.commit()
                    tgt["id"] = c.lastrowid

            mid = tgt["id"]
            chk = c.execute("SELECT start_time FROM meetings WHERE id=?", (mid,)).fetchone()
            st = datetime.fromisoformat(str(chk[0]).replace("Z", "+00:00"))
            if st.tzinfo is None:
                st = st.replace(tzinfo=timezone.utc)
            assert st < now, f"REFUSING: meeting {mid} is not in the past ({st})"

            blob = api_get(p["url"], timeout=180)
            d = os.path.join(args.pdfs, str(mid))
            os.makedirs(d, exist_ok=True)
            safe = re.sub(r"[^A-Za-z0-9._-]", "_",
                          f"{f['type']}_{f['fileId']}_{(f['name'] or 'doc').strip()}")[:120] + ".pdf"
            with open(os.path.join(d, safe), "wb") as fh:
                fh.write(blob)
            total_bytes += len(blob)

            text = None
            if p["doc_type"] == "agenda":
                # plainText=true returns real text for Agenda files only;
                # packets and minutes come back 200 with zero bytes.
                try:
                    t = api_get(p["url"].replace("plainText=false", "plainText=true"), timeout=120)
                    text = t.decode("utf-8", "replace") if t else None
                except Exception:
                    text = None

            c.execute("INSERT INTO documents (meeting_id,doc_type,original_url,local_path,"
                      "filename,extracted_text) VALUES (?,?,?,?,?,?)",
                      (mid, p["doc_type"], p["url"], f"{mid}/{safe}", safe, text))
            conn.commit()
            ok += 1
            if i % 25 == 0 or i == len(plan):
                print(f"  {i}/{len(plan)}  ok={ok} fail={fail}  {total_bytes/1024/1024:.0f}MB")
        except Exception as ex:
            fail += 1
            print(f"  FAIL fileId={f.get('fileId')}: {type(ex).__name__}: {ex}")

    conn.close()
    print(f"\ndone. inserted={ok} failed={fail} downloaded={total_bytes/1024/1024:.0f}MB")
    print("No email was sent: send_email / send_meeting_notification / "
          "check_upcoming_documents were never called.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
