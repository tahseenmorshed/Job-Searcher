"""HTTP API for the web frontend. Run from the project root: uvicorn api:app --reload"""

from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Literal

from fastapi import BackgroundTasks, FastAPI, HTTPException
from pydantic import BaseModel, Field

import assess_jev
import db
import fetch
import pipeline
import scoring
import scrapes
import spend
import tasks
from report import results


@asynccontextmanager
async def lifespan(app):
    conn = db.connect()
    tasks.fail_unfinished(conn)  # anything still "running" was cut off by the last shutdown
    scrapes.backfill(conn)       # fetches made before they were recorded, from their backup files
    conn.close()
    yield


app = FastAPI(lifespan=lifespan)


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


@app.get("/api/jev-requests/{job_id}")
def jev_requests(job_id: int, version: str):
    """Exactly what Jev was asked for one job under one version, with its answers."""
    conn = db.connect()
    found = assess_jev.load_requests(conn, job_id, version)
    if not found:
        raise HTTPException(404, f"Job {job_id} has no Jev result under {version}")
    return found


@app.get("/api/spend")
def spend_summary(recent: int = 20):
    """Money spent: totals by step and by month, and the latest runs (see spend.py)."""
    return spend.summary(db.connect(), recent)


# --- Tasks: work started from the page (see tasks.py) -------------------------------------------

class TaskRequest(BaseModel):
    """What to run. Each kind is one module's run() function; add the next one to WORK below."""
    kind: Literal["pipeline"]
    params: pipeline.PipelineParams = Field(default_factory=pipeline.PipelineParams)


class Approval(BaseModel):
    approve: bool


# Each kind's work, as tasks.execute expects: work(conn, task) -> JSON-safe result.
WORK = {
    "pipeline": lambda params: lambda conn, task: pipeline.run(conn, params, task),
}
ESTIMATE = {
    "pipeline": pipeline.estimate,
}


@app.get("/api/tasks/options")
def task_options():
    """The choices each kind accepts, with its defaults, so the page doesn't keep its own copy."""
    return {"fetch": fetch.options()}


@app.post("/api/tasks/estimate")
def estimate_task(req: TaskRequest):
    """What a task would cost, without starting it. Free."""
    return {"kind": req.kind, "params": req.params, **ESTIMATE[req.kind](db.connect(), req.params)}


@app.post("/api/tasks", status_code=202)
def start_task(req: TaskRequest, background: BackgroundTasks):
    """Start a task (this spends money) and return it at once; poll GET /api/tasks/{id} to follow it."""
    conn = db.connect()
    try:
        task_id = tasks.create(conn, req.kind, req.params.model_dump())
    except tasks.AlreadyRunning as e:
        raise HTTPException(409, f"A fetch is already running (task {e.task_id})")
    background.add_task(tasks.execute, task_id, WORK[req.kind](req.params))
    return tasks.get(conn, task_id)


@app.get("/api/tasks")
def list_tasks(limit: int = 20):
    """The latest tasks, newest first."""
    return tasks.recent(db.connect(), limit)


@app.get("/api/tasks/{task_id}")
def get_task(task_id: int):
    task = tasks.get(db.connect(), task_id)
    if not task:
        raise HTTPException(404, f"No task {task_id}")
    return task


@app.get("/api/tasks/{task_id}/log")
def task_log(task_id: int, after: int = 0):
    """The task's log lines after line `after`. Kept in memory only: empty after a restart."""
    t = tasks.live(task_id)
    if not t:
        return {"lines": [], "next": after}
    lines, total = t.lines_after(after)
    return {"lines": lines, "next": total}


@app.post("/api/tasks/{task_id}/approval")
def answer_task(task_id: int, body: Approval):
    """Approve or stop the paid step a task is waiting on."""
    t = tasks.live(task_id)
    if not t or not t.answer(body.approve):
        raise HTTPException(409, f"Task {task_id} isn't waiting for an answer")
    return {"ok": True}


@app.post("/api/tasks/{task_id}/abort")
def abort_task(task_id: int):
    """Stop a running task after its current job. What's done and paid for so far is kept."""
    t = tasks.live(task_id)
    task = tasks.get(db.connect(), task_id)
    if not t or not task or task["status"] not in tasks.UNFINISHED:
        raise HTTPException(409, f"Task {task_id} isn't running")
    t.abort()
    return {"ok": True}


@app.get("/api/scrapes")
def latest_scrapes():
    """When each city was last fetched from each source (see scrapes.py)."""
    return scrapes.latest(db.connect())
