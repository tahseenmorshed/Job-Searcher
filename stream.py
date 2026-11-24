"""The loop that runs the AI steps (triage, list requirements, rate) all at once, job by job.

One loop, on one thread, goes round in passes. Each pass it:
  1. asks every stage for the jobs ready for it (stage.module.find_work), skipping jobs whose call
     is already running or that failed earlier in this run,
  2. hands each new job to that stage's thread pool, which makes the API call (call_api),
  3. waits until any call, from any stage, comes back,
  4. saves each answer that came back (save_answer). That moves the job into the next stage's
     find_work, so the next pass picks it up.

Only this loop's thread touches the database: find_work and save_answer run here, one after the
other. The pools only make API calls. So there are no races between reading and writing, and the
database never sees two writers.

A job counts as done for a stage only once its result is saved, so nothing is missed: the loop
keeps asking until every stage comes back empty with nothing running. A call that fails isn't
saved, so its job is picked up again on the next run, as before.

A stage module provides:
  context()                                   context manager yielding what call_api needs (a client)
  find_work(conn, skip) -> [item]             items are dicts with at least "id" (the job id) and "job"
  call_api(ctx, item) -> (answer, cost_usd)   runs in the pool; never touches the database
  save_answer(conn, ctx, item, answer, run_id) -> (outcome, log line)
"""

import time
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import ExitStack
from dataclasses import dataclass, field
from typing import Any, Callable

import spend

# A stage whose calls fail this many times in a row stops the run: something is wrong for every
# job (no credit left, a bad key, the service down), and carrying on would only pile up errors.
MAX_ERRORS_IN_A_ROW = 10


@dataclass
class Stage:
    key: str                      # "triage", "requirements", "assess"
    label: str
    module: Any                   # see the module docstring
    limit: int                    # API calls in flight at once
    spend_step: str               # the runs table's step name, as spend.py knows it
    service: str                  # "anthropic", "typesafe"
    version: str
    model: str
    planned: int | None = None    # for the runs table: how many jobs the estimate expected
    estimate_usd: float | None = None


@dataclass
class Progress:
    """How far one stage has got. Updated by the loop as it goes."""
    done: int = 0                 # answers saved
    errors: int = 0               # calls (or saves) that failed: picked up again next run
    in_progress: int = 0          # calls running or queued in the pool right now
    finished: bool = False        # nothing left for it: the stages before it have finished too
    cost_usd: float = 0.0
    outcomes: Counter = field(default_factory=Counter)  # e.g. triage: {"core": 40, "adjacent": 61}
    started_at: float | None = None   # time.perf_counter() when its first call was handed out
    finished_at: float | None = None
    errors_in_a_row: int = 0


def process(conn, stages: list[Stage], *, should_stop: Callable[[], bool] = lambda: False,
            log: Callable[[str], None] = print,
            on_change: Callable[[dict[str, Progress]], None] | None = None,
            poll_s: float = 1.0):
    """Run every stage until none has work left. Returns ({stage key: Progress}, stopped).

    stopped is True when should_stop() turned true (or a stage kept failing): calls not yet
    started are cancelled, calls already running are saved when they finish, and each stage's
    spend record is marked aborted. Whatever is left is picked up on the next run.

    poll_s: how often the loop wakes, even with no answer back, to check should_stop."""
    progress = {s.key: Progress() for s in stages}
    in_progress = {}                           # future -> (stage, item)
    busy = {s.key: set() for s in stages}      # job ids with a call running or queued, per stage
    failed = {s.key: set() for s in stages}    # job ids that failed this run: not retried until next run
    stop_reason = None

    def start_new_work(ctx, pools):
        for i, s in enumerate(stages):
            p = progress[s.key]
            if p.finished:
                continue
            items = s.module.find_work(conn, busy[s.key] | failed[s.key])
            for item in items:
                future = pools[s.key].submit(s.module.call_api, ctx[s.key], item)
                in_progress[future] = (s, item)
                busy[s.key].add(item["id"])
                if p.started_at is None:
                    p.started_at = time.perf_counter()
            # Finished: nothing new, nothing running, and nothing more can arrive because every
            # stage before it has finished. The stages are checked in order, so an earlier stage
            # marked finished in this same pass counts.
            if not items and not busy[s.key] and all(progress[e.key].finished for e in stages[:i]):
                p.finished, p.finished_at = True, time.perf_counter()

    def collect(future, ctx, runs):
        s, item = in_progress.pop(future)
        p, run = progress[s.key], runs[s.key]
        title = item.get("job", {}).get("title", item["id"])
        try:
            answer, cost = future.result()
        except Exception as e:
            # Not saved, so it's picked up again next run. A call that was answered but then
            # failed (cut off, say) was still billed: its cost travels on the exception.
            fail(s, item, getattr(e, "cost_usd", 0.0) or 0.0, run)
            log(f"  {s.key:<12} ERROR {title}: {e}")
            return
        try:
            outcome, line = s.module.save_answer(conn, ctx[s.key], item, answer, run.id)
        except Exception as e:
            conn.rollback()  # drop anything half-written for this job
            fail(s, item, cost, run)
            log(f"  {s.key:<12} ERROR saving {title}: {e}")
            return
        finally:
            busy[s.key].discard(item["id"])
        run.add(cost, ok=1)
        p.done += 1
        p.cost_usd += cost
        p.outcomes[outcome] += 1
        p.errors_in_a_row = 0
        log(f"  {s.key:<12} {line}")

    def fail(s, item, cost, run):
        nonlocal stop_reason
        p = progress[s.key]
        busy[s.key].discard(item["id"])
        failed[s.key].add(item["id"])
        run.add(cost, failed=1)
        p.errors += 1
        p.cost_usd += cost
        p.errors_in_a_row += 1
        if p.errors_in_a_row >= MAX_ERRORS_IN_A_ROW and not stop_reason:
            stop_reason = (f"{s.label}: {p.errors_in_a_row} calls failed in a row, so the run is stopping. "
                           f"See the errors above.")

    def cancel_queued():
        """Cancel the calls the pools haven't started. Returns how many."""
        cancelled = 0
        for future in list(in_progress):
            if future.cancel():
                s, item = in_progress.pop(future)
                busy[s.key].discard(item["id"])
                cancelled += 1
        return cancelled

    stopped = False
    try:
        with ExitStack() as stack:
            ctx = {s.key: stack.enter_context(s.module.context()) for s in stages}
            runs = {s.key: stack.enter_context(spend.Run(
                conn, s.spend_step, s.service, version=s.version, model=s.model,
                planned=s.planned, estimate_usd=s.estimate_usd)) for s in stages}
            pools = {s.key: stack.enter_context(ThreadPoolExecutor(s.limit, thread_name_prefix=s.key))
                     for s in stages}
            try:
                stopping = False
                while True:
                    if not stopping and (stop_reason or should_stop()):
                        stopping = True
                        cancelled = cancel_queued()
                        log(stop_reason or "Stopping.")
                        log(f"{cancelled} calls not yet started were cancelled. {len(in_progress)} already "
                            f"running will be saved when they finish.")
                    if not stopping:
                        start_new_work(ctx, pools)
                    for s in stages:
                        progress[s.key].in_progress = len(busy[s.key])
                    if on_change:
                        on_change(progress)
                    if not in_progress:
                        break
                    done, _ = wait(in_progress, timeout=poll_s, return_when=FIRST_COMPLETED)
                    for future in done:
                        collect(future, ctx, runs)
                if stopping:
                    done_total = sum(p.done for p in progress.values())
                    raise spend.Aborted(done_total, done_total + sum(p.errors for p in progress.values()),
                                        sum(p.cost_usd for p in progress.values()))
            finally:
                # On an unexpected error, don't let queued calls run (and be paid for) unsaved.
                cancel_queued()
    except spend.Aborted:
        stopped = True
    for p in progress.values():
        p.in_progress = 0
    return progress, stopped
