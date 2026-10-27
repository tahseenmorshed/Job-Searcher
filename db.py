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

-- assess.py: how well each core job fits the resume. One row per job per assess version;
-- the score and rating are worked out from assessment_criteria when shown (see scoring.py).
CREATE TABLE IF NOT EXISTS assessments (
    id                    INTEGER PRIMARY KEY,
    job_id                INTEGER NOT NULL REFERENCES jobs(id),
    assess_version        TEXT NOT NULL,
    prompt_hash           TEXT,                  -- fingerprint of model + instructions + resume + schema
    model                 TEXT,                  -- the model that answered (differs if a fallback ran)
    triage_version        TEXT,                  -- the triage version that marked the job core
    clearance_evidence    TEXT,
    clearance_requirement TEXT,                  -- none / citizenship_only / obtainable / must_already_hold
    listing_detail        TEXT,                  -- detailed / sparse
    strengths             TEXT,                  -- JSON list
    gaps                  TEXT,                  -- JSON list of {gap, how_to_address}
    lead_with             TEXT,                  -- JSON list
    overall_reason        TEXT,
    model_rating          TEXT,                  -- the model's own overall rating, as a cross-check
    cost_usd              REAL,
    assessed_at           TEXT NOT NULL,
    UNIQUE (job_id, assess_version)
);

CREATE TABLE IF NOT EXISTS assessment_criteria (
    id                INTEGER PRIMARY KEY,
    assessment_id     INTEGER NOT NULL REFERENCES assessments(id) ON DELETE CASCADE,
    position          INTEGER NOT NULL,          -- order in the model's answer
    requirement       TEXT NOT NULL,
    listing_quote     TEXT,
    generic           INTEGER NOT NULL,          -- 0 / 1: boilerplate any applicant could claim
    importance_reason TEXT,
    importance        TEXT NOT NULL,             -- must_have / preferred / bonus
    flexible          INTEGER NOT NULL,          -- 0 / 1: "or similar", "or equivalent"
    resume_quotes     TEXT,                      -- JSON list of short fragments copied from the resume
    resume_evidence   TEXT,                      -- the model's own words on what the fragments show
    match_reason      TEXT,
    match             TEXT NOT NULL,             -- direct / transferable / side_project / stretch / none
    jev               TEXT                       -- JSON: Jev's probabilities and confidence (Jev versions only)
);

-- requirements.py: the requirements Haiku lists for each core job, before Jev rates them.
CREATE TABLE IF NOT EXISTS extractions (
    id                   INTEGER PRIMARY KEY,
    job_id               INTEGER NOT NULL REFERENCES jobs(id),
    requirements_version TEXT NOT NULL,          -- e.g. reqs-v1
    prompt_hash          TEXT,                   -- matches prompt_versions.prompt_hash (Haiku's instructions)
    model                TEXT,
    triage_version       TEXT,                   -- the triage version that marked the job core
    cost_usd             REAL,
    extracted_at         TEXT NOT NULL,
    UNIQUE (job_id, requirements_version)
);

CREATE TABLE IF NOT EXISTS requirements (
    id               INTEGER PRIMARY KEY,
    extraction_id    INTEGER NOT NULL REFERENCES extractions(id) ON DELETE CASCADE,
    position         INTEGER NOT NULL,
    listing_quote    TEXT,                       -- the listing sentence it came from
    requirement      TEXT NOT NULL,              -- a name that says on its own what's needed
    generic          INTEGER NOT NULL,           -- 0 / 1: boilerplate any applicant could claim
    haiku_importance TEXT                        -- Haiku's own guess, kept only to compare with Jev's
);

-- Every Jev request exactly as sent, with its raw answers, so any rating can be traced back to
-- the questions behind it (python assess_jev.py --show-requests <job id>).
CREATE TABLE IF NOT EXISTS jev_requests (
    id            INTEGER PRIMARY KEY,
    assessment_id INTEGER NOT NULL REFERENCES assessments(id) ON DELETE CASCADE,
    request_no    INTEGER NOT NULL,              -- 1 = reads the listing, 2 = reads the resume
    state         TEXT NOT NULL,                 -- JSON: what Jev read
    questions     TEXT NOT NULL,                 -- JSON: every question, as sent
    answers       TEXT NOT NULL,                 -- JSON: Jev's raw answers
    model         TEXT,
    input_tokens  INTEGER
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
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(assessment_criteria)")}
    if "resume_quotes" not in columns:
        conn.execute("ALTER TABLE assessment_criteria ADD COLUMN resume_quotes TEXT")
        conn.commit()
    if "jev" not in columns:
        conn.execute("ALTER TABLE assessment_criteria ADD COLUMN jev TEXT")
        conn.commit()
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(assessments)")}
    if "requirements_version" not in columns:
        # Which requirements.py version a Jev result rated (null: Sonnet's or Haiku's own list).
        conn.execute("ALTER TABLE assessments ADD COLUMN requirements_version TEXT")
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
