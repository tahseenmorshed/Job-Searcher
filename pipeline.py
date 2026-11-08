"""Fetch new listings, then triage them, list requirements and rate them: the page's Fetch button.

Each AI step first works out what it would do and what that costs, without spending anything
(its plan()), then asks before going ahead: the page shows the question with Continue and Stop,
like the scripts' y/N. With auto_approve, every step goes ahead without asking. Stopping skips
the remaining steps too, since each one works on what the one before produced.

Each step only picks up jobs it hasn't done yet, so this also catches up on jobs left over from
earlier fetches, and running it twice never pays for anything twice.
"""

import re

import spend

from pydantic import BaseModel, Field, model_validator

import assess_jev
import fetch
from fetch import FetchParams
import requirements
import triage

# The AI steps, in order. Each module has plan(conn, on_status=...) and run(conn, plan, log=...).
STEPS = [("triage", triage), ("requirements", requirements), ("assess", assess_jev)]
COUNTER = re.compile(r"\[(\d+)/(\d+)\]")  # the "[12/40]" each step logs per job


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
    """The fetch's likely cost (free). The AI steps are estimated as they're reached, once the
    fetch has shown how many new jobs there are."""
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


def run(conn, params: PipelineParams, task):
    """task: a tasks.Live, for the log, step list and questions."""
    steps = [("fetch", "Fetch listings")] if params.fetch else []
    if params.process:
        steps += [(key, module.Plan.label) for key, module in STEPS]
    task.plan_steps(steps)
    result = {}

    if params.fetch:
        task.log("--- Fetch listings ---")
        task.step("fetch", "running")

        def fetch_progress(message):
            task.progress(message)
            task.step("fetch", "running", message)

        try:
            r = fetch.run(conn, params.fetch, on_progress=fetch_progress, log=task.log)
        except Exception as e:
            task.step("fetch", "failed", str(e))
            raise
        result["fetch"] = r
        task.step("fetch", "done", f"{r['new']} new jobs · ${r['cost_usd']:.3f}")

    stopped = False
    for key, module in STEPS if params.process else []:
        label = module.Plan.label
        if task.aborted() and not stopped:
            task.log("Aborted.")
            result["aborted"] = stopped = True
        if stopped:
            task.step(key, "skipped", "Not run: aborted" if result.get("aborted") else "Not run: an earlier step was stopped")
            continue
        task.log(f"--- {label} ---")
        task.step(key, "running", "Working out the cost (free)")
        task.progress(f"{label}: working out the cost (free)")
        try:
            p = module.plan(conn, on_status=task.progress)
            free_work = getattr(p, "excluded", [])  # triage's title rejections cost nothing
            if not p.jobs and not free_work:
                task.step(key, "skipped", "Nothing new")
                task.log(f"{label}: nothing new to do")
                continue

            if p.jobs:
                summary = f"{p.jobs} jobs with {p.model}, estimated ${p.estimate_usd:.4f}"
                if p.warning:
                    task.log(f"WARNING: {p.warning}")
                if params.auto_approve:
                    task.log(f"{label}: {summary}. Approved automatically.")
                else:
                    task.log(f"{label}: {summary}. Waiting for approval.")
                    task.step(key, "waiting", summary)
                    task.progress(f"Waiting for your approval: {label}")
                    question = {
                        "step": key, "label": label, "jobs": p.jobs, "model": p.model, "version": p.version,
                        "input_tokens": p.input_tokens, "estimate_usd": p.estimate_usd, "warning": p.warning,
                        "note": (f"{len(free_work)} more rejected by title rule, free" if free_work
                                 else getattr(p, "note", None)),
                    }
                    if not task.ask(question):
                        result["aborted"] = task.aborted()
                        task.step(key, "stopped", "Stopped before spending anything")
                        task.log(f"{label}: stopped. Nothing was spent on this step.")
                        result[key] = {"stopped": True}
                        stopped = True
                        continue
                    task.log(f"{label}: approved.")

            task.step(key, "running", f"0 of {p.jobs}")
            task.progress(f"{label}: {p.jobs} jobs")

            def log(line, key=key, label=label):
                task.log(line)
                if m := COUNTER.search(line):
                    task.step(key, "running", f"{m.group(1)} of {m.group(2)}")
                    task.progress(f"{label}: {m.group(1)} of {m.group(2)}")

            r = module.run(conn, p, log=log, should_stop=task.aborted)
        except spend.Aborted as e:
            detail = f"Aborted after {e.done} of {e.total} · ${e.cost_usd:.4f}. The rest are picked up next run."
            task.step(key, "stopped", detail)
            task.log(f"{label}: {detail}")
            result[key] = {"aborted": True, "done": e.done, "total": e.total, "cost_usd": e.cost_usd}
            result["aborted"] = stopped = True
            continue
        except Exception as e:
            task.step(key, "failed", str(e))
            raise
        result[key] = r
        task.step(key, "done", _done_detail(key, r))
        task.log(f"{label}: {_done_detail(key, r)}")

    task.progress("Aborted" if result.get("aborted") else "Finished")
    result["steps"] = task.steps
    return result
