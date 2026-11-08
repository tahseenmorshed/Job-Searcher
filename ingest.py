"""Fetch Seek listings via Apify, save a JSON backup, and add them to data/jobseeker.db.

The work is done by run(), which the web API also calls (through fetch.py), so the script and
the page always fetch the same way.

Usage:
  python ingest.py                              # fetch with the defaults in SearchParams (paid)
  python ingest.py --days 7 --city Sydney --city Perth
  python ingest.py --dry-run                    # print the Seek URLs only
"""

import argparse
import json
import os
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Literal
from urllib.parse import urlencode

from apify_client import ApifyClient
from dotenv import load_dotenv
from pydantic import BaseModel, Field

import db
import dedupe
import scrapes
import spend

SOURCE = "seek"
ACTOR = "blackfalcondata/seek-scraper"
STEP = db.ACTOR_STEPS[ACTOR]          # how its cost is saved in the runs table
SEARCH_SLUG = "software-engineer"     # Seek URL form of the keyword search
STATE_KEY = "seek-se-test"            # the actor remembers which jobs this key has already seen
BACKUP_DIR = Path("data")

# Each city as Seek's own location slug.
SEEK_LOCATIONS = {"Perth": "All-Perth-WA", "Melbourne": "All-Melbourne-VIC", "Sydney": "All-Sydney-NSW"}
# Only these values are accepted, so a bad request can't start an unexpected (paid) search.
Days = Literal[1, 3, 7, 14, 31]       # Seek's own date filter options


class SearchParams(BaseModel):
    """What to fetch. The defaults are what a plain `python ingest.py` fetches."""
    cities: list[db.City] = Field(default=["Perth", "Melbourne"], min_length=1)
    days: Days = 3
    max_pages: int = Field(default=25, ge=1, le=25)  # per city, ~22 jobs per page
    max_results: int = Field(default=0, ge=0)        # 0: no cap


@dataclass
class IngestResult:
    source: str
    log_urls: list[str]
    items: int                     # everything the actor returned
    new: int                       # jobs added to the database
    existing: int                  # jobs already there (updated, not duplicated)
    by_city: dict[str, int]        # jobs found in each city searched
    backup: str
    cost_usd: float | None
    duplicates_hidden: int | None = None  # None when the caller checks duplicates itself
    possible_duplicates: int | None = None
    extra: dict = field(default_factory=dict)  # source-specific detail for the script's printout
    jobs: list[dict] = field(default_factory=list, repr=False)  # the listings, for the script to print

    def summary(self):
        """Everything but the listings, small enough to save with a task."""
        return {k: v for k, v in self.__dict__.items() if k not in ("jobs", "extra")} | {"jobs_found": len(self.jobs)}


def seek_urls(params):
    query = urlencode({"daterange": params.days, "sortmode": "ListedDate"})
    return [f"https://au.seek.com/{SEARCH_SLUG}-jobs/in-{SEEK_LOCATIONS[city]}?{query}"
            for city in dict.fromkeys(params.cities)]


def run_input(params):
    return {
        "startUrls": seek_urls(params),
        "country": "AU",
        "maxResults": params.max_results,
        "maxPages": params.max_pages,
        "includeDetails": True,
        "descriptionFormat": "all",
        "incrementalMode": True,
        "stateKey": STATE_KEY,
    }


def recent_costs(conn):
    """Apify bills by what it scrapes, so a run's cost isn't known in advance: what recent Seek
    fetches cost is the best guide. Each run covered every city asked for."""
    return [row[0] for row in conn.execute(
        """SELECT cost_usd FROM runs WHERE step = ? AND status IN ('done', 'backfilled')
           ORDER BY started_at DESC LIMIT 10""", (STEP,))]


def run(conn, params: SearchParams, on_progress: Callable[[str], None] = print, check_duplicates=True,
        log: Callable[[str], None] | None = None) -> IngestResult:
    """Scrape Seek (paid), back up the results, save them and, unless the caller will, hide duplicates."""
    load_dotenv(".env")
    token = os.environ.get("APIFY_TOKEN")
    if not token:
        raise RuntimeError("APIFY_TOKEN is missing from .env")
    client = ApifyClient(token)

    cities = list(dict.fromkeys(params.cities))
    on_progress(f"Searching {', '.join(cities)}, past {params.days} days. This usually takes a few minutes.")
    apify_run = client.actor(ACTOR).call(run_input=run_input(params))
    if apify_run is None:
        raise RuntimeError(f"{ACTOR} didn't start")
    cost = apify_run.usage_total_usd

    # Recorded straight away: the money is spent even if saving fails below. Apify can settle the
    # final figure a little later; python spend.py --backfill refreshes it.
    items = list(client.dataset(apify_run.default_dataset_id).iterate_items())
    with spend.Run(conn, STEP, "apify", model=ACTOR, external_id=apify_run.id) as spent:
        spent.add(cost or 0.0, ok=sum(1 for item in items if item.get("seekJobId")))
    on_progress(f"Scraper finished with {len(items)} items. Saving them.")

    # Keep only job records (skips any run-summary item), deduped across cities by Seek's job number.
    jobs = list({item["seekJobId"]: item for item in items if item.get("seekJobId")}.values())
    by_city = {city: sum(1 for job in jobs if f"/in-{SEEK_LOCATIONS[city]}?" in (job.get("searchUrl") or ""))
               for city in cities}

    now = datetime.now()
    backup = BACKUP_DIR / f"seek_{now:%Y%m%d_%H%M%S}.json"
    backup.parent.mkdir(exist_ok=True)
    backup.write_text(json.dumps(jobs, indent=2, ensure_ascii=False))

    # New jobs get a row; jobs already there are updated, never duplicated.
    new, existing = db.save_jobs(conn, SOURCE, jobs, now.isoformat(timespec="seconds"))
    scrapes.record(conn, SOURCE, by_city, params.days, now.isoformat(timespec="seconds"), spent.id, backup.name)
    on_progress(f"Done: {new} new jobs, {existing} already saved.")
    if log:  # the page's log window; the script prints its own list afterwards
        for job in jobs:
            log(f"  Seek · {job.get('title')} | {job.get('company')} | {job.get('location')}")

    result = IngestResult(
        source=SOURCE, log_urls=[f"https://console.apify.com/view/runs/{apify_run.id}"], items=len(items),
        new=new, existing=existing, by_city=by_city, backup=str(backup), cost_usd=cost, jobs=jobs,
        extra={
            # What the scraper says it actually searched, and how jobs changed since the last run.
            "by_search_url": dict(Counter(item.get("searchUrl") for item in items if item.get("seekJobId"))),
            "by_state": dict(Counter(job.get("locationState") for job in jobs)),
            "by_change_type": dict(Counter(job.get("changeType") for job in jobs)),
        },
    )
    if check_duplicates:
        result.duplicates_hidden, result.possible_duplicates = dedupe.check(conn, quiet=True)  # free
    return result


def main():
    defaults = SearchParams()
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="print the Seek URLs and stop")
    ap.add_argument("--city", action="append", dest="cities", choices=db.CITIES,
                    help=f"repeatable (default: {' '.join(defaults.cities)})")
    ap.add_argument("--days", type=int, default=defaults.days, help="1, 3, 7, 14 or 31")
    ap.add_argument("--max-pages", type=int, default=defaults.max_pages)
    args = ap.parse_args()
    params = SearchParams(cities=args.cities or defaults.cities, days=args.days, max_pages=args.max_pages)

    if args.dry_run:
        print("\n".join(seek_urls(params)))
        return

    print("Search URLs:")
    for url in seek_urls(params):
        print(f"  {url}")
    r = run(db.connect(), params)

    print("\nSearch URLs reported by the scraper (items per URL):")
    for url, n in r.extra["by_search_url"].items():
        print(f"  {n:>4}  {url}")
    print(f"Run log: {r.log_urls[0]}\n")
    for job in r.jobs:
        print(f"- {job.get('title')} | {job.get('company')} | {job.get('location')} | {job.get('postedDate')}")
    print(f"\nBy city:        {r.by_city}")
    print(f"By state:       {r.extra['by_state']}")
    print(f"By change type: {r.extra['by_change_type']}  (repeat-run mode)")
    print(f"\n{r.items} items, {len(r.jobs)} unique jobs -> backup {r.backup}")
    print(f"Database: {r.new} new, {r.existing} already there -> {db.DB_PATH}")
    print(f"Duplicates: {r.duplicates_hidden} listings hidden, {r.possible_duplicates} possible duplicates "
          f"to review (python dedupe.py --show)")
    if r.cost_usd is not None:
        print(f"Cost: ${r.cost_usd:.3f}  (all spend: python spend.py)")


if __name__ == "__main__":
    main()
