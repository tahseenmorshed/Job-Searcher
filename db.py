"""Shared database code: the schema, one converter per job source, and saving jobs.

Every source (Seek now, LinkedIn/Indeed later) has a converter that turns its own JSON record
into the same set of columns, so evaluate.py and report.py never see source-specific fields.
"""

import json
import sqlite3
from pathlib import Path

DB_PATH = Path("data/jobseeker.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id               INTEGER PRIMARY KEY,
    source           TEXT NOT NULL,              -- 'seek', later 'linkedin', 'indeed'
    source_job_id    TEXT NOT NULL,              -- the site's own ID for the job
    title            TEXT NOT NULL,
    company          TEXT,
    location         TEXT,
    url              TEXT,
    posted_at        TEXT,                       -- ISO date/time
    work_arrangement TEXT,                       -- e.g. remote / hybrid / onsite
    employment_type  TEXT,                       -- e.g. Full time / Contract/Temp
    salary_text      TEXT,
    description      TEXT,                       -- plain text
    raw_json         TEXT NOT NULL,              -- the original record, untouched
    first_seen_at    TEXT NOT NULL,
    last_seen_at     TEXT NOT NULL,
    duplicate_of     INTEGER REFERENCES jobs(id), -- for cross-site duplicates, later
    UNIQUE (source, source_job_id)
);

CREATE TABLE IF NOT EXISTS evaluations (
    id                          INTEGER PRIMARY KEY,
    job_id                      INTEGER NOT NULL REFERENCES jobs(id),
    prompt_version              TEXT NOT NULL,
    prompt_hash                 TEXT,            -- fingerprint of model + instructions + profile + schema
    model                       TEXT,
    title_status                TEXT,            -- pass / combo / excluded
    title_term                  TEXT,
    relevance                   TEXT,            -- core / adjacent / not_relevant
    relevance_reason            TEXT,
    open_to_software_background INTEGER,         -- 0 / 1
    open_background_evidence    TEXT,
    min_years_required          INTEGER,
    years_evidence              TEXT,
    decision                    TEXT,            -- keep / reject
    reason                      TEXT,
    error                       TEXT,
    cost_usd                    REAL,
    evaluated_at                TEXT NOT NULL,
    UNIQUE (job_id, prompt_version)              -- each job triaged once per prompt version
);

-- The exact prompt behind each version, so any result can be traced to the text that produced it.
-- One row per fingerprint: if a version is continued after its text changed, both are kept.
CREATE TABLE IF NOT EXISTS prompt_versions (
    prompt_hash    TEXT PRIMARY KEY,             -- fingerprint, matches evaluations.prompt_hash
    prompt_version TEXT NOT NULL,
    model          TEXT,
    notes          TEXT,                         -- what changed in this version
    system_text    TEXT,                         -- full instructions including profile.md
    schema_json    TEXT,                         -- the Triage schema sent to the API
    first_used_at  TEXT NOT NULL
);
"""


def connect():
    """Open the database, creating the file and tables if they don't exist yet."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row  # rows behave like dicts: row["title"]
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    # Columns added after the database was first created. CREATE TABLE IF NOT EXISTS doesn't
    # change an existing table, so add them here if they're missing.
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(evaluations)")}
    if "prompt_hash" not in columns:
        conn.execute("ALTER TABLE evaluations ADD COLUMN prompt_hash TEXT")
        conn.commit()
    return conn


# --- Converters: one per source, all returning the same keys -------------------------------

def seek_to_row(raw):
    return {
        "source_job_id": str(raw["seekJobId"]),
        "title": raw.get("title") or "",
        "company": raw.get("company"),
        "location": raw.get("location"),
        "url": raw.get("canonicalUrl"),
        "posted_at": raw.get("postedDate"),
        "work_arrangement": raw.get("workArrangement"),
        "employment_type": raw.get("employmentType"),
        "salary_text": raw.get("salaryText"),
        "description": raw.get("description") or raw.get("teaser"),
    }


CONVERTERS = {"seek": seek_to_row}


def save_jobs(conn, source, records, seen_at):
    """Add new jobs and refresh existing ones. Returns (new_count, existing_count).

    A job already in the table (same source + source_job_id) keeps its row and first_seen_at;
    its details and last_seen_at are updated to the latest version.
    """
    convert = CONVERTERS[source]
    new = existing = 0
    for raw in records:
        row = convert(raw)
        found = conn.execute(
            "SELECT id FROM jobs WHERE source = ? AND source_job_id = ?",
            (source, row["source_job_id"]),
        ).fetchone()
        if found:
            conn.execute(
                """UPDATE jobs SET title = :title, company = :company, location = :location, url = :url,
                       posted_at = :posted_at, work_arrangement = :work_arrangement,
                       employment_type = :employment_type, salary_text = :salary_text,
                       description = :description, raw_json = :raw_json, last_seen_at = :seen_at
                   WHERE id = :id""",
                {**row, "raw_json": json.dumps(raw, ensure_ascii=False), "seen_at": seen_at, "id": found["id"]},
            )
            existing += 1
        else:
            conn.execute(
                """INSERT INTO jobs (source, source_job_id, title, company, location, url, posted_at,
                       work_arrangement, employment_type, salary_text, description, raw_json,
                       first_seen_at, last_seen_at)
                   VALUES (:source, :source_job_id, :title, :company, :location, :url, :posted_at,
                       :work_arrangement, :employment_type, :salary_text, :description, :raw_json,
                       :seen_at, :seen_at)""",
                {**row, "source": source, "raw_json": json.dumps(raw, ensure_ascii=False), "seen_at": seen_at},
            )
            new += 1
    conn.commit()
    return new, existing
