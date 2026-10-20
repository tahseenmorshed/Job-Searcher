"""One-time import of the existing Seek JSON files and triage results into data/jobseeker.db.

Safe to run more than once: jobs are matched on (source, source_job_id) and results on
(job, prompt_version), so nothing is duplicated.
"""

import json
from datetime import datetime
from pathlib import Path

import db

conn = db.connect()

# 1. Seek files, oldest first, so first_seen_at reflects when each job first appeared.
#    The timestamp comes from the file name: seek_20261017_165230.json -> 2026-10-17T16:52:30
for path in sorted(Path("data").glob("seek_*.json")):
    seen_at = datetime.strptime(path.stem.removeprefix("seek_"), "%Y%m%d_%H%M%S").isoformat()
    records = json.loads(path.read_text())
    new, existing = db.save_jobs(conn, "seek", records, seen_at)
    print(f"{path.name}: {len(records)} records -> {new} new, {existing} already in the database")

# 2. Triage results, oldest first; a later result for the same job and prompt version replaces an earlier one.
for path in sorted(Path("data/evaluations").glob("*.jsonl")):
    imported = missing = 0
    for line in path.read_text().splitlines():
        r = json.loads(line)
        job = conn.execute(
            "SELECT id FROM jobs WHERE source = 'seek' AND source_job_id = ?", (str(r["seekJobId"]),)
        ).fetchone()
        if not job:
            missing += 1
            continue
        t = r.get("triage") or {}
        conn.execute(
            """INSERT OR REPLACE INTO evaluations (job_id, prompt_version, model, title_status, title_term,
                   relevance, relevance_reason, open_to_software_background, open_background_evidence,
                   min_years_required, years_evidence, decision, reason, error, cost_usd, evaluated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)""",
            (job["id"], r["prompt_version"], r["model"], r["title_status"], r["title_term"],
             t.get("relevance"), t.get("relevance_reason"), t.get("open_to_software_background"),
             t.get("open_background_evidence"), t.get("min_years_required"), t.get("years_evidence"),
             r["decision"], r["reason"], r["error"], r["evaluated_at"]),
        )
        imported += 1
    conn.commit()
    print(f"{path.name}: {imported} results imported" + (f", {missing} skipped (job not found)" if missing else ""))

jobs = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
evals = conn.execute("SELECT COUNT(*) FROM evaluations").fetchone()[0]
print(f"\n{db.DB_PATH}: {jobs} jobs, {evals} triage results")
