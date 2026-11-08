"""Fetch LinkedIn listings via Apify, save a JSON backup, add them to data/jobseeker.db, then
check for duplicates (dedupe.py).

The work is done by run(), which the web API also calls (through fetch.py).

Usage:
  python ingest_linkedin.py                         # scrape (paid) and save
  python ingest_linkedin.py --days 7 --city Sydney
  python ingest_linkedin.py --dry-run               # print the search inputs only
  python ingest_linkedin.py --from-file data/x.json # load a saved backup instead of scraping (free)
"""

import argparse
import json
import os
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Callable, Literal

from apify_client import ApifyClient
from dotenv import load_dotenv
from pydantic import BaseModel, Field

import db
import dedupe
import scrapes
import spend
from ingest import IngestResult

SOURCE = "linkedin"
ACTOR = "valig/linkedin-jobs-scraper"
STEP = db.ACTOR_STEPS[ACTOR]
KEYWORDS = "software engineer"
BACKUP_DIR = Path("data")

# The actor takes one free-text location per run, so each city is its own run.
LINKEDIN_LOCATIONS = {
    "Perth": "Perth, Western Australia, Australia",
    "Melbourne": "Melbourne, Victoria, Australia",
    "Sydney": "Sydney, New South Wales, Australia",
}
# The actor's datePosted setting only accepts these, so these are the timeframes on offer.
DATE_POSTED = {1: "r86400", 7: "r604800", 30: "r2592000"}
Days = Literal[1, 7, 30]


class SearchParams(BaseModel):
    """What to fetch. The defaults are what a plain `python ingest_linkedin.py` fetches."""
    cities: list[db.City] = Field(default=["Perth", "Melbourne"], min_length=1)
    days: Days = 1
    limit: int = Field(default=1000, ge=1, le=1000)  # jobs per city; 1000 is the actor's maximum


def run_input(params, city):
    return {"keywords": KEYWORDS, "location": LINKEDIN_LOCATIONS[city],
            "datePosted": DATE_POSTED[params.days], "limit": params.limit}


def recent_costs(conn):
    """What recent LinkedIn runs cost. Each run is one city, so a fetch costs about this per city."""
    return [row[0] for row in conn.execute(
        """SELECT cost_usd FROM runs WHERE step = ? AND status IN ('done', 'backfilled')
           ORDER BY started_at DESC LIMIT 10""", (STEP,))]


def save(conn, jobs, seen_at):
    """New jobs get a row; jobs already there are updated, never duplicated."""
    return db.save_jobs(conn, SOURCE, jobs, seen_at)


def run(conn, params: SearchParams, on_progress: Callable[[str], None] = print, check_duplicates=True,
        log: Callable[[str], None] | None = None) -> IngestResult:
    """Scrape LinkedIn (paid), one run per city, back up the results, save them and, unless the
    caller will, hide duplicates."""
    load_dotenv(".env")
    token = os.environ.get("APIFY_TOKEN")
    if not token:
        raise RuntimeError("APIFY_TOKEN is missing from .env")
    client = ApifyClient(token)

    cities = list(dict.fromkeys(params.cities))
    items, found, run_ids, log_urls, cost = [], {}, {}, [], 0.0
    for i, city in enumerate(cities, 1):
        on_progress(f"Searching {city} ({i} of {len(cities)}), past {params.days} "
                    f"{'day' if params.days == 1 else 'days'}.")
        apify_run = client.actor(ACTOR).call(run_input=run_input(params, city))
        if apify_run is None:
            raise RuntimeError(f"{ACTOR} didn't start for {city}")
        city_items = list(client.dataset(apify_run.default_dataset_id).iterate_items())
        for item in city_items:
            item["searchLocation"] = LINKEDIN_LOCATIONS[city]  # which search found it, since the actor doesn't say
        items += city_items
        found[city] = len(city_items)
        log_urls.append(f"https://console.apify.com/view/runs/{apify_run.id}")
        # Recorded straight away; Apify can settle the final figure later (python spend.py --backfill).
        with spend.Run(conn, STEP, "apify", model=ACTOR, external_id=apify_run.id) as spent:
            spent.add(apify_run.usage_total_usd or 0.0, ok=len(city_items))
        run_ids[city] = spent.id
        cost += apify_run.usage_total_usd or 0.0

    # Keep only job records, deduped across cities by LinkedIn's job ID.
    jobs = list({item["id"]: item for item in items if item.get("id")}.values())

    now = datetime.now()
    backup = BACKUP_DIR / f"linkedin_{now:%Y%m%d_%H%M%S}.json"
    backup.parent.mkdir(exist_ok=True)
    backup.write_text(json.dumps(jobs, indent=2, ensure_ascii=False))

    new, existing = save(conn, jobs, now.isoformat(timespec="seconds"))
    for city in cities:
        scrapes.record(conn, SOURCE, {city: found[city]}, params.days, now.isoformat(timespec="seconds"),
                       run_ids[city], backup.name)
    on_progress(f"Done: {new} new jobs, {existing} already saved.")
    if log:  # the page's log window; the script prints its own list afterwards
        for job in jobs:
            log(f"  LinkedIn · {job.get('title')} | {job.get('companyName')} | {job.get('location')}")

    result = IngestResult(
        source=SOURCE, log_urls=log_urls, items=len(items), new=new, existing=existing, by_city=found,
        backup=str(backup), cost_usd=cost, jobs=jobs,
    )
    if check_duplicates:
        result.duplicates_hidden, result.possible_duplicates = dedupe.check(conn, quiet=True)
    return result


def main():
    defaults = SearchParams()
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="print the search inputs and stop")
    ap.add_argument("--from-file", type=Path, help="load a saved backup instead of scraping (free)")
    ap.add_argument("--city", action="append", dest="cities", choices=db.CITIES,
                    help=f"repeatable (default: {' '.join(defaults.cities)})")
    ap.add_argument("--days", type=int, default=defaults.days, help="1, 7 or 30")
    args = ap.parse_args()
    params = SearchParams(cities=args.cities or defaults.cities, days=args.days)

    if args.dry_run:
        for city in params.cities:
            print(json.dumps(run_input(params, city)))
        return

    conn = db.connect()
    if args.from_file:
        # The file's own timestamp (linkedin_YYYYmmdd_HHMMSS.json) is when the jobs were seen.
        seen = datetime.strptime(args.from_file.stem.removeprefix("linkedin_"), "%Y%m%d_%H%M%S")
        jobs = json.loads(args.from_file.read_text())
        print(f"{len(jobs)} jobs from {args.from_file}")
        new, existing = save(conn, jobs, seen.isoformat(timespec="seconds"))
        print(f"Database: {new} new, {existing} already there -> {db.DB_PATH}")
        dedupe.check(conn)
        sys.exit()

    r = run(conn, params)
    print()
    for job in r.jobs:
        print(f"- {job.get('title')} | {job.get('companyName')} | {job.get('location')} | {job.get('postedDate')}")
    print(f"\nBy city: {r.by_city}")
    print(f"Run logs: {' '.join(r.log_urls)}")
    print(f"{r.items} items, {len(r.jobs)} unique jobs -> backup {r.backup}")
    print(f"Database: {r.new} new, {r.existing} already there -> {db.DB_PATH}")
    print(f"Duplicates: {r.duplicates_hidden} listings hidden, {r.possible_duplicates} possible duplicates "
          f"to review (python dedupe.py --show)")
    print(f"Cost: ${r.cost_usd:.3f}  (all spend: python spend.py)")


if __name__ == "__main__":
    main()
