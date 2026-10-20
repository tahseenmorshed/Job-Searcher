"""Fetch Seek listings via Apify, save a JSON backup, and add them to data/jobseeker.db."""

import json
import os
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode

from apify_client import ApifyClient
from dotenv import load_dotenv

import db

ACTOR = "blackfalcondata/seek-scraper"
SEARCH_SLUG = "software-engineer"  # Seek URL form of the keyword search
# Seek's own location slugs. Full list: ["All-Perth-WA", "All-Melbourne-VIC", "All-Sydney-NSW"]
LOCATIONS = ["All-Perth-WA", "All-Melbourne-VIC"]  # Perth only while testing the database

DATE_RANGE = 3   # days
MAX_PAGES = 25   # per URL, ~22 jobs per page
MAX_RESULT = 0

def seek_url(location):
    params = urlencode({"daterange": DATE_RANGE, "sortmode": "ListedDate"})
    return f"https://au.seek.com/{SEARCH_SLUG}-jobs/in-{location}?{params}"


if "--dry-run" in sys.argv:
    for loc in LOCATIONS:
        print(seek_url(loc))
    sys.exit()

load_dotenv()
client = ApifyClient(os.environ["APIFY_TOKEN"])

run_input = {
    "startUrls": [seek_url(loc) for loc in LOCATIONS],
    "country": "AU",
    "maxResults": MAX_RESULT,         
    "maxPages": MAX_PAGES,
    "includeDetails": True,   
    "descriptionFormat": "all",
    "incrementalMode": True,  # turn on once output looks right
    "stateKey": "seek-se-test"
}

print(f"Running {ACTOR} for {len(LOCATIONS)} locations with these URLs:")
for url in run_input["startUrls"]:
    print(f"  {url}")
run = client.actor(ACTOR).call(run_input=run_input)
items = list(client.dataset(run.default_dataset_id).iterate_items())

# What the scraper says it actually searched, and where to read its log.
print("\nSearch URLs reported by the scraper (items per URL):")
for url, n in Counter(item.get("searchUrl") for item in items if item.get("seekJobId")).items():
    print(f"  {n:>4}  {url}")
print(f"Run log: https://console.apify.com/view/runs/{run.id}\n")

# Keep only job records (skips any run-summary item), deduped across cities by Seek's job number.
jobs = list({item["seekJobId"]: item for item in items if item.get("seekJobId")}.values())

now = datetime.now()
out = Path("data") / f"seek_{now:%Y%m%d_%H%M%S}.json"
out.parent.mkdir(exist_ok=True)
out.write_text(json.dumps(jobs, indent=2, ensure_ascii=False))

for job in jobs:
    print(f"- {job.get('title')} | {job.get('company')} | {job.get('location')} | {job.get('postedDate')}")

# Add to the database: new jobs get a row; jobs already there are updated, never duplicated.
conn = db.connect()
new, existing = db.save_jobs(conn, "seek", jobs, now.isoformat(timespec="seconds"))

print(f"\nBy state:       {dict(Counter(job.get('locationState') for job in jobs))}")
print(f"By change type: {dict(Counter(job.get('changeType') for job in jobs))}  (repeat-run mode)")
print(f"\n{len(items)} items, {len(jobs)} unique jobs -> backup {out}")
print(f"Database: {new} new, {existing} already there -> {db.DB_PATH}")
if run.usage_total_usd is not None:
    print(f"Cost: ${run.usage_total_usd:.3f}")
