"""HTTP API for the web frontend. Run from the project root: uvicorn api:app --reload"""

from datetime import datetime, timedelta

from fastapi import FastAPI

import db
import scoring
from report import results

app = FastAPI()


def latest_version(conn):
    row = conn.execute("SELECT prompt_version FROM evaluations ORDER BY evaluated_at DESC LIMIT 1").fetchone()
    return row[0] if row else None


@app.get("/api/versions")
def versions():
    """Prompt versions that have results, newest first."""
    conn = db.connect()
    rows = conn.execute("""SELECT prompt_version, MAX(evaluated_at) AS last_used FROM evaluations
                           GROUP BY prompt_version ORDER BY last_used DESC""")
    return [row["prompt_version"] for row in rows]


@app.get("/api/assess-versions")
def assess_versions():
    """Assessment versions that have results (e.g. assess-v1, assess-v1-haiku), newest first."""
    conn = db.connect()
    rows = conn.execute("""SELECT assess_version, MAX(assessed_at) AS last_used FROM assessments
                           GROUP BY assess_version ORDER BY last_used DESC""")
    return [row["assess_version"] for row in rows]


@app.get("/api/jobs")
def jobs(version: str | None = None, assess_version: str | None = None, new: bool = False):
    """Triage results for one prompt version (default: the latest), each with its assessment
    from one assess version (default: the latest)."""
    conn = db.connect()
    version = version or latest_version(conn)
    since = (datetime.now() - timedelta(days=1)).isoformat(timespec="seconds") if new else None
    rows = results(conn, version, since) if version else []
    assess_version = assess_version or scoring.latest_version(conn)
    assessed = scoring.rated_assessments(conn, assess_version) if assess_version else {}
    for row in rows:
        row["assessment"] = assessed.get(row["id"])
    return {"version": version, "assess_version": assess_version, "jobs": rows}
