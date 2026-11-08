"""When each city was last fetched from each source, for the page's "Last fetched" table.

Each fetch records one row per city it searched (record). Fetches made before this was recorded
are rebuilt from their backup files in data/ (backfill), which the API does when it starts.

Usage:
  python scrapes.py              # latest fetch per source and city
  python scrapes.py --backfill   # add rows for any backup file not recorded yet (free, safe to rerun)
"""

import argparse
import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path

import db

BACKUP_DIR = Path("data")

# How each source's backup records say which city was searched.
SEEK_SLUG_CITY = {"All-Perth-WA": "Perth", "All-Melbourne-VIC": "Melbourne", "All-Sydney-NSW": "Sydney"}
LINKEDIN_STATE_CITY = {"Western Australia": "Perth", "Victoria": "Melbourne", "New South Wales": "Sydney"}


def record(conn, source, found, days, scraped_at, run_id=None, backup=None):
    """found: {city: jobs found}, including cities that found nothing."""
    conn.executemany(
        """INSERT OR IGNORE INTO scrapes (source, city, days, scraped_at, jobs_found, run_id, backup)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        [(source, city, days, scraped_at, n, run_id, backup) for city, n in found.items()])
    conn.commit()


def latest(conn):
    """The latest fetch of each source and city."""
    return [dict(row) for row in conn.execute(
        """SELECT source, city, days, scraped_at, jobs_found FROM scrapes s
           WHERE scraped_at = (SELECT MAX(scraped_at) FROM scrapes
                               WHERE source = s.source AND city = s.city)
           ORDER BY source, city""")]


def _from_backup(path):
    """(source, {city: jobs}, days) from one backup file, or None if it can't tell."""
    source = path.stem.split("_")[0]
    jobs = json.loads(path.read_text())
    if source == "seek":
        found, days = Counter(), None
        for job in jobs:
            url = job.get("searchUrl") or ""
            slug = re.search(r"/in-([\w-]+)\?", url)
            if slug and slug.group(1) in SEEK_SLUG_CITY:
                found[SEEK_SLUG_CITY[slug.group(1)]] += 1
            days = days or (int(m.group(1)) if (m := re.search(r"daterange=(\d+)", url)) else None)
        return source, dict(found), days
    if source == "linkedin":
        found = Counter(LINKEDIN_STATE_CITY[state] for job in jobs
                        for state in LINKEDIN_STATE_CITY if state in (job.get("searchLocation") or ""))
        return source, dict(found), 1  # every LinkedIn fetch so far searched the past 24 hours
    return None


def backfill(conn):
    """Add rows for backup files the table doesn't know yet. Returns how many files were added."""
    known = {row[0] for row in conn.execute("SELECT DISTINCT backup FROM scrapes WHERE backup IS NOT NULL")}
    added = 0
    for path in sorted(BACKUP_DIR.glob("*_????????_??????.json")):
        if path.name in known:
            continue
        try:
            parsed = _from_backup(path)
        except (ValueError, OSError):
            continue
        if not parsed or not parsed[1]:
            continue  # an empty file doesn't say which cities were searched
        source, found, days = parsed
        stamp = datetime.strptime(path.stem.split("_", 1)[1], "%Y%m%d_%H%M%S")
        record(conn, source, found, days, stamp.isoformat(timespec="seconds"), backup=path.name)
        added += 1
    return added


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backfill", action="store_true")
    args = ap.parse_args()
    conn = db.connect()
    if args.backfill:
        print(f"Added {backfill(conn)} backup files")
    for row in latest(conn):
        print(f"  {row['source']:<9} {row['city']:<10} {row['scraped_at']}  past {row['days']} days, "
              f"{row['jobs_found']} jobs")


if __name__ == "__main__":
    main()
