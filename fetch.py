"""Fetch from one or more sources at once: the page's "Fetch listings" button (POST /api/tasks).

Each source is scraped in its own thread, since most of the time is spent waiting on Apify.
Duplicates are checked once at the end, across everything that came in. One source failing
doesn't stop the other.
"""

from concurrent.futures import ThreadPoolExecutor
from typing import Callable, get_args

from pydantic import BaseModel, Field, model_validator

import db
import ingest
import ingest_linkedin
import dedupe

SOURCES = {"seek": ingest, "linkedin": ingest_linkedin}
LABELS = {"seek": "Seek", "linkedin": "LinkedIn"}


class SeekChoice(BaseModel):
    days: ingest.Days = 3


class LinkedInChoice(BaseModel):
    days: ingest_linkedin.Days = 1


class FetchParams(BaseModel):
    """Which cities, and which sources with each one's timeframe. A source left out isn't fetched."""
    cities: list[db.City] = Field(default=["Perth", "Melbourne"], min_length=1)
    seek: SeekChoice | None = Field(default_factory=SeekChoice)
    linkedin: LinkedInChoice | None = None

    @model_validator(mode="after")
    def one_source(self):
        if not self.seek and not self.linkedin:
            raise ValueError("pick at least one source")
        return self

    def source_params(self):
        """{source: that module's SearchParams} for each source picked."""
        picked = {}
        if self.seek:
            picked["seek"] = ingest.SearchParams(cities=self.cities, days=self.seek.days)
        if self.linkedin:
            picked["linkedin"] = ingest_linkedin.SearchParams(cities=self.cities, days=self.linkedin.days)
        return picked


def options():
    """The choices the page offers, with its defaults."""
    return {
        "cities": list(db.CITIES),
        "sources": {
            "seek": {"label": "Seek", "days": list(get_args(ingest.Days)), "default_days": SeekChoice().days},
            "linkedin": {"label": "LinkedIn", "days": list(get_args(ingest_linkedin.Days)),
                         "default_days": LinkedInChoice().days},
        },
        "defaults": FetchParams().model_dump(),
    }


def _range(costs):
    return {"estimate_usd": sum(costs) / len(costs) if costs else None, "low_usd": min(costs, default=None),
            "high_usd": max(costs, default=None), "based_on_runs": len(costs)}


def _longest_searched(conn, source):
    row = conn.execute("SELECT MAX(days) FROM scrapes WHERE source = ?", (source,)).fetchone()
    return row[0]


def estimate(conn, params: FetchParams):
    """What a fetch is likely to cost, from what recent fetches cost (free). Apify bills by what it
    scrapes, so the real figure is only known afterwards."""
    cities = len(set(params.cities))
    by_source = {}
    for source, p in params.source_params().items():
        costs = SOURCES[source].recent_costs(conn)
        e = _range(costs)
        if source == "linkedin" and e["estimate_usd"] is not None:
            # One run per city, so the cost grows with each city.
            e = {k: (v * cities if k.endswith("_usd") and v is not None else v) for k, v in e.items()}
        longest = _longest_searched(conn, source)
        e["longer_than_before"] = longest is not None and p.days > longest
        e["longest_searched_days"] = longest
        by_source[source] = e
    known = [e["estimate_usd"] for e in by_source.values() if e["estimate_usd"] is not None]
    return {"by_source": by_source, "total_usd": sum(known) if len(known) == len(by_source) else None}


def run(conn, params: FetchParams, on_progress: Callable[[str], None] = print,
        log: Callable[[str], None] | None = None):
    """Fetch every source picked, then check for duplicates. Returns each source's summary or error.
    Raises only if every source failed."""
    picked = params.source_params()
    status = {source: "Waiting to start" for source in picked}

    def report(source):
        def progress(message):
            status[source] = message
            on_progress("\n".join(f"{LABELS[s]}: {m}" for s, m in status.items()))
            if log:
                log(f"{LABELS[source]}: {message}")
        return progress

    def fetch_one(source, p):
        own = db.connect()  # each thread needs its own connection
        try:
            return SOURCES[source].run(own, p, report(source), check_duplicates=False, log=log).summary()
        finally:
            own.close()

    results = {}
    with ThreadPoolExecutor(max_workers=len(picked)) as pool:
        futures = {source: pool.submit(fetch_one, source, p) for source, p in picked.items()}
        for source, future in futures.items():
            try:
                results[source] = {"ok": True, **future.result()}
            except Exception as e:
                report(source)(f"Failed: {e}")
                results[source] = {"ok": False, "error": f"{type(e).__name__}: {e}"}

    if not any(r["ok"] for r in results.values()):
        raise RuntimeError("; ".join(f"{LABELS[s]}: {r['error']}" for s, r in results.items()))
    on_progress("Checking for duplicates across sources")
    hidden, possible = dedupe.check(conn, quiet=True)
    if log:
        log(f"Duplicates: {hidden} listings hidden, {possible} possible duplicates to review")
    return {"by_source": results, "duplicates_hidden": hidden, "possible_duplicates": possible,
            "new": sum(r.get("new", 0) for r in results.values()),
            "cost_usd": sum(r.get("cost_usd") or 0 for r in results.values())}
