"""Shared database code: the schema, one converter per job source, and saving jobs.

Every source (Seek now, LinkedIn/Indeed later) has a converter that turns its own JSON record
into the same set of columns, so evaluate.py and report.py never see source-specific fields.
"""

import json
import re
import sqlite3
from html.parser import HTMLParser
from pathlib import Path
from typing import Literal

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
    kind             TEXT,                       -- skill / duty (reqs-v2 on; null before)
    generic         INTEGER NOT NULL,           -- 0 / 1: boilerplate any applicant could claim
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

-- Money spent: one row per run of a paid script (see spend.py). The row is written when you agree
-- to the estimate and updated after every job, so a crash or Ctrl-C still keeps what was spent.
CREATE TABLE IF NOT EXISTS runs (
    id            INTEGER PRIMARY KEY,
    step          TEXT NOT NULL,                 -- the script: ingest / evaluate / requirements / assess / assess_jev
    service       TEXT NOT NULL,                 -- who bills it: apify / anthropic / typesafe
    version       TEXT,                          -- e.g. triage-v3, reqs-v2, assess-v3-jev
    model         TEXT,                          -- the model, or the Apify actor
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    status        TEXT NOT NULL,                 -- running / done / aborted / backfilled
    items_planned INTEGER,                       -- jobs it set out to process
    items_ok      INTEGER NOT NULL DEFAULT 0,
    items_failed  INTEGER NOT NULL DEFAULT 0,
    estimate_usd  REAL,                          -- the estimate shown before you agreed
    cost_usd      REAL NOT NULL DEFAULT 0,       -- includes calls that were billed but then failed
    external_id   TEXT UNIQUE                    -- Apify run ID
);

-- dedupe.py: pairs of listings that look like the same job, from either site. job_id is the
-- earlier of the two (lower id). Rebuilt on every check, except pairs you've reviewed.
CREATE TABLE IF NOT EXISTS job_matches (
    id           INTEGER PRIMARY KEY,
    job_id       INTEGER NOT NULL REFERENCES jobs(id),
    other_job_id INTEGER NOT NULL REFERENCES jobs(id),
    similarity   REAL NOT NULL,                  -- description similarity, 0 to 1
    same_title   INTEGER NOT NULL,               -- 0 / 1: titles match after cleanup
    status       TEXT NOT NULL,                  -- duplicate (one is hidden) / possible (both kept)
    review       TEXT,                           -- your verdict, kept across checks: same / different
    checked_at   TEXT NOT NULL,
    UNIQUE (job_id, other_job_id)
);

-- tasks.py: work started from the web page, such as a Seek fetch. The page polls a task's row to
-- follow it. What a task spent is in runs, like any script's.
CREATE TABLE IF NOT EXISTS tasks (
    id          INTEGER PRIMARY KEY,
    kind        TEXT NOT NULL,                   -- what it does, e.g. ingest_seek
    params      TEXT NOT NULL,                   -- JSON: what was asked for (e.g. locations, days)
    status      TEXT NOT NULL,                   -- queued / running / done / failed
    progress    TEXT,                            -- the latest step, shown on the page
    result      TEXT,                            -- JSON: what it did, once done
    error       TEXT,                            -- why it failed
    created_at  TEXT NOT NULL,
    started_at  TEXT,
    finished_at TEXT
);

-- scrapes.py: one row per city per source each time it was searched, even if nothing was found,
-- so the page can show when each was last fetched. Rows from before this table existed are
-- rebuilt from the backup files in data/ (those only show cities that returned jobs).
CREATE TABLE IF NOT EXISTS scrapes (
    id          INTEGER PRIMARY KEY,
    source      TEXT NOT NULL,                   -- seek / linkedin
    city        TEXT NOT NULL,                   -- Perth / Melbourne / Sydney (CITIES below)
    days        INTEGER,                         -- how far back it searched
    scraped_at  TEXT NOT NULL,
    jobs_found  INTEGER NOT NULL,
    run_id      INTEGER REFERENCES runs(id),     -- what it cost (null for rebuilt rows)
    backup      TEXT,                            -- the backup file in data/
    UNIQUE (source, city, scraped_at)
);
"""


# The cities that can be fetched, by the names the page and scrapes table use. Each source module
# maps them to that site's own location format (SEEK_LOCATIONS, LINKEDIN_LOCATIONS).
CITIES = ("Perth", "Melbourne", "Sydney")
City = Literal["Perth", "Melbourne", "Sydney"]


# The Apify actor behind each source, so each site's scraping cost is recorded as its own step
# in the runs table (spend.py). Runs of any other actor are saved under plain 'ingest'.
ACTOR_STEPS = {
    "blackfalcondata/seek-scraper": "ingest_seek",
    "valig/linkedin-jobs-scraper": "ingest_linkedin",
}


def connect():
    """Open the database, creating the file and tables if they don't exist yet."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row  # rows behave like dicts: row["title"]
    conn.execute("PRAGMA foreign_keys = ON")
    # Lets the page keep reading while a background task writes. Saved in the file, so this only
    # changes anything the first time.
    conn.execute("PRAGMA journal_mode = WAL")
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
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(requirements)")}
    if "kind" not in columns:
        conn.execute("ALTER TABLE requirements ADD COLUMN kind TEXT")
        conn.commit()
    # Which run paid for each result (null: saved without an AI call, or before runs were recorded).
    for table in ("evaluations", "extractions", "assessments"):
        if "run_id" not in {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN run_id INTEGER REFERENCES runs(id)")
            conn.commit()
    # Scraping runs were once all saved as 'ingest'; give each the step for its site.
    # Checked with a read first, so a normal connect never takes a write lock.
    for actor, step in ACTOR_STEPS.items():
        if conn.execute("SELECT 1 FROM runs WHERE step = 'ingest' AND model = ?", (actor,)).fetchone():
            conn.execute("UPDATE runs SET step = ? WHERE step = 'ingest' AND model = ?", (step, actor))
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


# LinkedIn locations look like "Docklands, Victoria, Australia" or "Greater Melbourne Area".
# They're rewritten the way Seek writes them ("Docklands, Melbourne VIC"), so the web app groups
# jobs from both sites under the same city. Keyed by state, plus the city's own name.
LINKEDIN_CITIES = {
    "Western Australia": "Perth WA",
    "Victoria": "Melbourne VIC",
    "New South Wales": "Sydney NSW",
}


def linkedin_location(location, search_location):
    """'Docklands, Victoria, Australia' -> 'Docklands, Melbourne VIC'; 'Greater Perth Area' -> 'Perth WA'."""
    parts = [p.strip() for p in (location or "").split(",")]
    # The state named in the location, else the state of the city that was searched.
    state = next((p for p in parts if p in LINKEDIN_CITIES), None)
    if not state:
        state = next((p for p in (search_location or "").split(", ") if p in LINKEDIN_CITIES), None)
    city = LINKEDIN_CITIES.get(state)
    if not city:
        return location or None  # somewhere this table doesn't cover: keep LinkedIn's wording
    city_name = city.rsplit(" ", 1)[0]  # 'Melbourne VIC' -> 'Melbourne'
    suburb = parts[0]
    if len(parts) < 3 or suburb == city_name or re.fullmatch(r"Greater .* Area", suburb):
        return city
    return f"{suburb}, {city}"


# LinkedIn's contractType, in Seek's wording.
LINKEDIN_EMPLOYMENT = {
    "Full-time": "Full time",
    "Part-time": "Part time",
    "Contract": "Contract/Temp",
    "Temporary": "Contract/Temp",
    "Internship": "Internship",
}


class _HtmlText(HTMLParser):
    """Plain text from HTML, with a line break wherever a paragraph, list item or heading ends."""
    BLOCKS = {"p", "div", "br", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "tr"}

    def __init__(self):
        super().__init__()
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag == "li":
            self.parts.append("\n- ")
        elif tag in self.BLOCKS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.BLOCKS:
            self.parts.append("\n")

    def handle_data(self, data):
        self.parts.append(data)


def html_to_text(html):
    parser = _HtmlText()
    parser.feed(html or "")
    text = "".join(parser.parts).replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def linkedin_to_row(raw):
    # LinkedIn's plain-text description runs paragraphs together, so it's rebuilt from the HTML.
    description = html_to_text(raw.get("descriptionHtml")) or raw.get("description")
    return {
        "source_job_id": str(raw["id"]),
        "title": raw.get("title") or "",
        "company": raw.get("companyName") or None,
        "location": linkedin_location(raw.get("location"), raw.get("searchLocation")),
        "url": raw.get("url"),
        "posted_at": raw.get("postedDate"),
        "work_arrangement": None,  # this actor doesn't report remote / hybrid / onsite
        "employment_type": LINKEDIN_EMPLOYMENT.get(raw.get("contractType"), raw.get("contractType") or None),
        "salary_text": raw.get("salary") or None,
        "description": description or None,
    }


CONVERTERS = {"seek": seek_to_row, "linkedin": linkedin_to_row}


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
