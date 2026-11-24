"""Fetch new listings, then triage them, list requirements and rate them: the page's Fetch button.

After the fetch, it works out what the three AI steps should cost, without spending anything, and
asks once: the page shows the question with Continue and Stop. With auto_approve it goes ahead
without asking. Then the three steps run together (stream.py): each job moves on to the next step
as soon as it's done with the one before, rather than every job waiting for the whole step.

Each step only picks up jobs it hasn't done yet, so this also catches up on jobs left over from
earlier fetches, and running it twice never pays for anything twice.
"""

import json
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

import spend

from pydantic import BaseModel, Field, model_validator

import assess_jev
import fetch
from fetch import FetchParams
import requirements
import stream
import triage

TIMINGS_DIR = Path("data/timings")  # one JSON file per run: how long each part took, and every job's outcome

# The AI steps, in order: (key, label, module, the runs table's step name, service).
AI_STEPS = [
    ("triage", "Triage", triage, "evaluate", "anthropic"),
    ("requirements", "List requirements", requirements, "requirements", "anthropic"),
    ("assess", "Rate with Jev", assess_jev, "assess_jev", "typesafe"),
]
# API calls each step may have in flight at once. Lower these if you hit rate limits.
LIMITS = {"triage": 10, "requirements": 10, "assess": 8}
# For the estimate, until this database has history of its own (enough results under the current
# versions): the share of triaged jobs that turn out core, and the average cost per job, from the
# 2026-11-22 run (146 core of 484 triaged).
DEFAULT_CORE_SHARE = 0.30
FALLBACK_COST_PER_JOB = {"triage": 0.00025, "requirements": 0.00175, "assess": 0.00045}


class PipelineParams(BaseModel):
    fetch: FetchParams | None = Field(default_factory=FetchParams)  # None: only process saved jobs
    process: bool = True        # triage, list requirements and rate after fetching
    auto_approve: bool = False  # go ahead with every paid AI step without asking

    @model_validator(mode="after")
    def something_to_do(self):
        if not self.fetch and not self.process:
            raise ValueError("nothing to do: fetch, process, or both")
        return self


def estimate(conn, params: PipelineParams):
    """The fetch's likely cost (free). The AI steps are estimated after the fetch, once it has
    shown how many new jobs there are."""
    if not params.fetch:
        return {"by_source": {}, "total_usd": 0.0}
    return fetch.estimate(conn, params.fetch)


def _done_detail(key, r):
    errors = f", {r['errors']} errors" if r.get("errors") else ""
    if key == "triage":
        c = r["counts"]
        rejected = c["too_much_experience"] + c["not_relevant"] + c["title_excluded"]
        return (f"{c['core']} core, {c['adjacent'] + c['open_background']} adjacent, {rejected} rejected{errors}"
                f" · ${r['cost_usd']:.4f}")
    if key == "requirements":
        return f"{r['listed']} listed{errors} · ${r['cost_usd']:.4f}"
    return f"{r['rated']} rated{errors} · ${r['cost_usd']:.4f}"


class Clock:
    """Times a run and each part of it. Waiting for approval is timed on its own, so it can be
    left out when comparing runs."""

    def __init__(self):
        self.started_at = datetime.now().isoformat(timespec="seconds")
        self._start = time.perf_counter()
        self.parts = {}  # "fetch", "triage.plan", "triage.approval_wait", "triage.run", ...

    @contextmanager
    def time(self, name):
        t = time.perf_counter()
        try:
            yield
        finally:
            self.parts[name] = round(self.parts.get(name, 0.0) + time.perf_counter() - t, 2)

    def total(self):
        return round(time.perf_counter() - self._start, 2)

    def at(self, perf_counter_time):
        """A time.perf_counter() reading, as seconds since the run started."""
        return round(perf_counter_time - self._start, 2)


def run(conn, params: PipelineParams, task):
    """task: a tasks.Live, for the log, step list and questions. Times the whole run and saves
    the timings with every job's outcome to TIMINGS_DIR, even if the run fails or is stopped."""
    clock = Clock()
    result = {}
    try:
        result = _run(conn, params, task, clock)
        return result
    finally:
        try:
            _record_timing(conn, params, task, clock, result)
        except Exception as e:  # never hide the run's own result or error
            task.log(f"Couldn't save the timing record: {e}")


def _job_outcomes(conn):
    """Every saved job with its triage category and how far it got, for comparing runs job by job."""
    rows = conn.execute(
        """SELECT j.source, j.source_job_id, j.title, j.company, j.duplicate_of IS NOT NULL AS duplicate,
                  e.reason AS triage,
                  EXISTS (SELECT 1 FROM extractions x WHERE x.job_id = j.id AND x.requirements_version = ?) AS listed,
                  EXISTS (SELECT 1 FROM assessments a WHERE a.job_id = j.id AND a.assess_version = ?) AS rated
           FROM jobs j LEFT JOIN evaluations e ON e.job_id = j.id AND e.prompt_version = ?
           ORDER BY j.source, j.source_job_id""",
        (requirements.REQUIREMENTS_VERSION, assess_jev.VERSION, triage.PROMPT_VERSION))
    jobs = []
    for r in rows:
        job = dict(r)
        job["category"] = "duplicate" if job["duplicate"] else (job["triage"] or "untriaged")
        job["duplicate"], job["listed"], job["rated"] = bool(job["duplicate"]), bool(job["listed"]), bool(job["rated"])
        del job["triage"]
        jobs.append(job)
    return jobs


def _record_timing(conn, params, task, clock, result):
    total = clock.total()
    waited = round(sum(s for name, s in clock.parts.items() if name.endswith(".approval_wait")), 2)
    jobs = _job_outcomes(conn)
    categories = {}
    for job in jobs:
        categories[job["category"]] = categories.get(job["category"], 0) + 1
    record = {
        "started_at": clock.started_at,
        "params": params.model_dump(mode="json"),
        "aborted": bool(result.get("aborted")),
        "total_s": total,
        "approval_wait_s": waited,
        "total_excluding_approval_s": round(total - waited, 2),
        "parts_s": clock.parts,
        "counts": {
            "jobs": len(jobs),
            "by_category": dict(sorted(categories.items())),
            "listed": sum(j["listed"] for j in jobs),
            "rated": sum(j["rated"] for j in jobs),
        },
        "jobs": jobs,
    }
    TIMINGS_DIR.mkdir(parents=True, exist_ok=True)
    path = TIMINGS_DIR / f"run-{clock.started_at.replace(':', '').replace('-', '')}.json"
    path.write_text(json.dumps(record, indent=1))

    task.log("--- Timing ---")
    task.log(f"Total {total:.1f}s, of which {waited:.1f}s waiting for approval: "
             f"{total - waited:.1f}s of work")
    for name, seconds in clock.parts.items():
        task.log(f"  {name:<28} {seconds:>8.1f}s")
    task.log(f"Jobs by category: {record['counts']['by_category']} · "
             f"{record['counts']['listed']} listed · {record['counts']['rated']} rated")
    task.log(f"Saved to {path}")


def _run(conn, params: PipelineParams, task, clock: Clock):
    steps = [("fetch", "Fetch listings")] if params.fetch else []
    if params.process:
        steps += [(key, label) for key, label, *_ in AI_STEPS]
    task.plan_steps(steps)
    result = {}

    if params.fetch:
        task.log("--- Fetch listings ---")
        task.step("fetch", "running")

        def fetch_progress(message):
            task.progress(message)
            task.step("fetch", "running", message)

        try:
            with clock.time("fetch"):
                r = fetch.run(conn, params.fetch, on_progress=fetch_progress, log=task.log)
        except Exception as e:
            task.step("fetch", "failed", str(e))
            raise
        result["fetch"] = r
        task.step("fetch", "done", f"{r['new']} new jobs · ${r['cost_usd']:.3f}")

    if params.process:
        _process(conn, params, task, clock, result)

    task.progress("Aborted" if result.get("aborted") else "Finished")
    result["steps"] = task.steps
    return result


def _process(conn, params: PipelineParams, task, clock: Clock, result):
    """Ask once for all three AI steps, then run them together (stream.py). Fills in result."""
    def all_steps(status, detail):
        for key, *_ in AI_STEPS:
            task.step(key, status, detail)

    if task.aborted():
        all_steps("skipped", "Not run: aborted")
        result["aborted"] = True
        return

    task.log("--- Triage, list requirements and rate ---")
    task.step("triage", "running", "Working out the cost (free)")
    task.progress("Working out the cost (free)")
    with clock.time("estimate"):
        # With auto-approve nobody sees the estimate, so skip the exact (slow) token count.
        tp = triage.plan(conn, on_status=task.progress, count_tokens=not params.auto_approve)
        est = _estimate(conn, tp)
    paid_jobs = tp.jobs + est["left_over"]
    if not paid_jobs and not tp.excluded:
        all_steps("skipped", "Nothing new")
        task.log("Nothing new to do.")
        return

    warnings = [w for w in (tp.warning, requirements.prompt_warning(conn), assess_jev.prompt_warning(conn)) if w]
    for w in warnings:
        task.log(f"WARNING: {w}")
    if paid_jobs:
        summary = f"{paid_jobs} jobs, estimated ${est['total_usd']:.4f}"
        task.log(f"{summary}: {est['note']}")
        if params.auto_approve:
            task.log("Approved automatically.")
        else:
            task.step("triage", "waiting", summary)
            task.progress("Waiting for your approval")
            question = {
                "step": "triage", "label": "Triage, list requirements and rate", "jobs": paid_jobs,
                "model": f"{triage.MODEL} and Jev",
                "version": f"{triage.PROMPT_VERSION} · {requirements.REQUIREMENTS_VERSION} · {assess_jev.VERSION}",
                "input_tokens": tp.input_tokens, "estimate_usd": est["total_usd"],
                "warning": " ".join(warnings) or None,
                "note": est["note"] + (f" · {len(tp.excluded)} more rejected by title, free" if tp.excluded else ""),
            }
            with clock.time("approval_wait"):
                approved = task.ask(question)
            if not approved:
                result["aborted"] = task.aborted()
                all_steps("stopped", "Stopped before spending anything")
                task.log("Stopped. Nothing was spent.")
                return
            task.log("Approved.")

    triage.record_prompt(conn)
    requirements.record_prompt(conn)
    triage.save_excluded(conn, tp.excluded, task.log)
    if not paid_jobs:  # only title rejections, which cost nothing
        result["triage"] = _stage_result("triage", stream.Progress(), len(tp.excluded))
        task.step("triage", "done", _done_detail("triage", result["triage"]))
        task.step("requirements", "skipped", "Nothing new")
        task.step("assess", "skipped", "Nothing new")
        return

    stages = [stream.Stage(key, label, module, LIMITS[key], spend_step, service, module.Plan.version,
                           module.Plan.model, planned=est[key]["jobs"], estimate_usd=est[key]["usd"])
              for key, label, module, spend_step, service in AI_STEPS]
    last_progress = [0.0]

    def show(progress):
        for key, p in progress.items():
            if p.finished:
                task.step(key, "done", _done_detail(key, _stage_result(key, p, len(tp.excluded))))
            elif p.started_at is None:
                task.step(key, "pending", "Waiting for jobs from the step before")
            else:
                errors = f" · {p.errors} errors" if p.errors else ""
                task.step(key, "running", f"{p.done} done · {p.in_progress} in progress{errors}")
        if time.monotonic() - last_progress[0] >= 1:  # saved to the database, so not every pass
            last_progress[0] = time.monotonic()
            task.progress(" · ".join(f"{label}: {progress[key].done}" for key, label, *_ in AI_STEPS))

    try:
        with clock.time("process"):
            progress, stopped = stream.process(conn, stages, should_stop=task.aborted, log=task.log, on_change=show)
    except Exception as e:
        all_steps("failed", str(e))
        raise

    for key, label, *_ in AI_STEPS:
        p = progress[key]
        result[key] = _stage_result(key, p, len(tp.excluded))
        if p.finished_at:  # when each step finished, counted from the start of the run
            clock.parts[f"{key}.finished_after"] = clock.at(p.finished_at)
        if p.finished:
            detail = _done_detail(key, result[key])
            task.step(key, "done", detail)
        else:
            errors = f", {p.errors} errors" if p.errors else ""
            detail = f"Stopped after {p.done}{errors} · ${p.cost_usd:.4f}. The rest are picked up next run."
            task.step(key, "stopped", detail)
        task.log(f"{label}: {detail}")
    if stopped:
        result["aborted"] = True


def _stage_result(key, p: stream.Progress, title_excluded=0):
    """A step's summary, in the shape the page and _done_detail expect."""
    if key == "triage":
        counts = {reason: p.outcomes.get(reason, 0) for reason, _ in triage.SUMMARY}
        return {"counts": counts | {"title_excluded": title_excluded, "errors": p.errors}, "cost_usd": p.cost_usd}
    if key == "requirements":
        return {"listed": p.done, "errors": p.errors, "cost_usd": p.cost_usd}
    return {"rated": p.done, "errors": p.errors, "cost_usd": p.cost_usd}


def _avg_cost(conn, table, version_column, version, key):
    """Average cost per job saved under this version, or the fallback until there are 10."""
    row = conn.execute(f"SELECT AVG(cost_usd), COUNT(*) FROM {table} WHERE {version_column} = ? AND cost_usd > 0",
                       (version,)).fetchone()
    return (row[0], True) if row[1] >= 10 else (FALLBACK_COST_PER_JOB[key], False)


def _estimate(conn, tp):
    """What the AI steps should cost (free). Triage is counted exactly when tp has a token count.
    Requirements and Jev can't be: how many jobs reach them depends on triage, so they're estimated
    from the share of triaged jobs that turn out core, and the average cost per job, both taken from
    this database's history once it has enough, otherwise from the 2026-11-22 run."""
    row = conn.execute("""SELECT COUNT(*), SUM(reason = 'core') FROM evaluations
                          WHERE prompt_version = ? AND model IS NOT NULL""", (triage.PROMPT_VERSION,)).fetchone()
    share, share_known = (row[1] / row[0], True) if row[0] >= 50 else (DEFAULT_CORE_SHARE, False)
    triage_each, _ = _avg_cost(conn, "evaluations", "prompt_version", triage.PROMPT_VERSION, "triage")
    req_each, req_known = _avg_cost(conn, "extractions", "requirements_version", requirements.REQUIREMENTS_VERSION,
                                    "requirements")
    jev_each, jev_known = _avg_cost(conn, "assessments", "assess_version", assess_jev.VERSION, "assess")

    left_req = len(requirements.find_work(conn))  # core jobs from earlier runs, not yet listed
    left_assess = len(assess_jev.find_work(conn))  # listed earlier, not yet rated
    req_jobs = left_req + round(tp.jobs * share)
    assess_jobs = left_assess + req_jobs
    triage_usd = tp.estimate_usd if tp.input_tokens else tp.jobs * triage_each
    est = {
        "triage": {"jobs": tp.jobs, "usd": triage_usd},
        "requirements": {"jobs": req_jobs, "usd": req_jobs * req_each},
        "assess": {"jobs": assess_jobs, "usd": assess_jobs * jev_each},
        "left_over": left_req + left_assess,
    }
    est["total_usd"] = sum(est[k]["usd"] for k in ("triage", "requirements", "assess"))
    basis = "this database's past runs" if share_known and req_known and jev_known else "the 2026-11-22 run"
    parts = []
    if tp.jobs:
        parts.append(f"triage {tp.jobs} jobs ${triage_usd:.4f}{' (counted)' if tp.input_tokens else ''}")
    parts.append(f"list ~{req_jobs} (${est['requirements']['usd']:.4f}) and rate ~{assess_jobs} "
                 f"(${est['assess']['usd']:.4f}), assuming {share:.0%} turn out core, estimated from {basis}")
    est["note"] = " · ".join(parts)
    return est
