"""Money spent by each paid step, saved per run so it can be looked at later.

Each paid script opens a Run once you agree to the estimate, and adds to it after every job.
Calls that were billed but then failed (a cut-off answer, say) are counted too.

Usage:
  python spend.py              # totals by step and by month, and the latest runs
  python spend.py --runs 30    # show the last 30 runs
  python spend.py --backfill   # create runs for results saved before runs were recorded, and
                               # fetch every Apify run's cost. Safe to run again: it only adds
                               # what's missing and refreshes Apify costs.
"""

import argparse
import os
import sys
from datetime import datetime

import db

# The paid scripts, in pipeline order, with how each is shown.
STEPS = {
    "ingest_seek": "Fetch listings · Seek",
    "ingest_linkedin": "Fetch listings · LinkedIn",
    "ingest": "Fetch listings · other Apify",
    "evaluate": "Triage · Haiku",
    "requirements": "List requirements · Haiku",
    "assess": "Assess · Claude (older)",
    "assess_jev": "Rate · Jev",
}


def now():
    return datetime.now().isoformat(timespec="seconds")


class BilledError(RuntimeError):
    """A call that failed after it was charged for, carrying its cost so it can still be recorded."""

    def __init__(self, message, cost_usd):
        super().__init__(message)
        self.cost_usd = cost_usd


class Aborted(Exception):
    """A run stopped part way because it was asked to (the page's Abort button). Raised inside
    `with spend.Run(...)`, so the run is marked aborted with what was spent so far kept."""

    def __init__(self, done, total, cost_usd):
        super().__init__(f"aborted after {done} of {total}")
        self.done, self.total, self.cost_usd = done, total, cost_usd


def check_abort(should_stop, done, total, run):
    """Called before each job: stop here if asked to. Jobs already done are saved."""
    if should_stop and should_stop():
        raise Aborted(done, total, run.cost_usd)


class Run:
    """One run of a paid script. Use as `with spend.Run(...) as run:`; an exception or Ctrl-C
    inside marks it aborted, with everything spent up to that point kept."""

    def __init__(self, conn, step, service, *, version=None, model=None, planned=None,
                 estimate_usd=None, external_id=None):
        self.conn = conn
        self.cost_usd = 0.0
        self.id = conn.execute(
            """INSERT INTO runs (step, service, version, model, started_at, status, items_planned,
                   estimate_usd, external_id)
               VALUES (?, ?, ?, ?, ?, 'running', ?, ?, ?)""",
            (step, service, version, model, now(), planned, estimate_usd, external_id),
        ).lastrowid
        conn.commit()

    def add(self, cost_usd=0.0, ok=0, failed=0):
        """Record spend and finished items straight away."""
        self.cost_usd += cost_usd or 0.0
        self.conn.execute(
            """UPDATE runs SET cost_usd = cost_usd + ?, items_ok = items_ok + ?, items_failed = items_failed + ?
               WHERE id = ?""",
            (cost_usd or 0.0, ok, failed, self.id),
        )
        self.conn.commit()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.conn.execute("UPDATE runs SET status = ?, finished_at = ? WHERE id = ?",
                          ("aborted" if exc_type else "done", now(), self.id))
        self.conn.commit()
        return False


# --- Reading it back (used here and by api.py) --------------------------------------------------

def summary(conn, recent=10):
    month = datetime.now().strftime("%Y-%m")
    total = conn.execute("SELECT COALESCE(SUM(cost_usd), 0) FROM runs").fetchone()[0]
    this_month = conn.execute(
        "SELECT COALESCE(SUM(cost_usd), 0) FROM runs WHERE substr(started_at, 1, 7) = ?", (month,)).fetchone()[0]
    by_step = {row["step"]: dict(row) for row in conn.execute(
        """SELECT step, COUNT(*) AS runs, SUM(cost_usd) AS cost_usd, SUM(items_ok) AS items,
                  MAX(started_at) AS last_run
           FROM runs GROUP BY step""")}
    months = {}
    for row in conn.execute(
            """SELECT substr(started_at, 1, 7) AS month, step, SUM(cost_usd) AS cost_usd
               FROM runs GROUP BY month, step ORDER BY month DESC"""):
        m = months.setdefault(row["month"], {"month": row["month"], "cost_usd": 0.0, "by_step": {}})
        m["cost_usd"] += row["cost_usd"]
        m["by_step"][row["step"]] = row["cost_usd"]
    runs = [dict(row) for row in conn.execute("SELECT * FROM runs ORDER BY started_at DESC, id DESC LIMIT ?",
                                              (recent,))]
    for r in runs:
        r["label"] = STEPS.get(r["step"], r["step"])
    return {
        "total_usd": total,
        "this_month_usd": this_month,
        "month": month,
        "by_step": [{**by_step[s], "label": label} for s, label in STEPS.items() if s in by_step],
        "by_month": list(months.values()),
        "recent": runs,
        "unlinked": unlinked(conn),
        "by_source": by_source(conn),
    }


SOURCE_LABELS = {"seek": "Seek", "linkedin": "LinkedIn", "unknown": "Not tied to a source"}


def by_source(conn):
    """Everything spent because of each job site: its scraping, plus the AI steps run on its jobs.

    A scraping run belongs to its site. An AI run's bill is split across sites in proportion to
    the jobs it processed, so caching and failed calls are shared out too. AI runs with no saved
    results (deleted later) from before any other site was added count as Seek, the only site then;
    later ones can't be placed and show as unknown.
    """
    first_other = conn.execute("SELECT MIN(first_seen_at) FROM jobs WHERE source != 'seek'").fetchone()[0]
    totals = {}

    def add(source, step, cost):
        t = totals.setdefault(source, {"source": source, "cost_usd": 0.0, "by_step": {}})
        t["cost_usd"] += cost
        t["by_step"][step] = t["by_step"].get(step, 0.0) + cost

    for run in conn.execute("SELECT id, step, started_at, cost_usd FROM runs WHERE cost_usd > 0").fetchall():
        if run["step"].startswith("ingest_"):
            add(run["step"].removeprefix("ingest_"), run["step"], run["cost_usd"])
            continue
        counts = dict(conn.execute(
            """SELECT j.source, COUNT(*) FROM jobs j JOIN (
                   SELECT job_id FROM evaluations WHERE run_id = :id
                   UNION ALL SELECT job_id FROM extractions WHERE run_id = :id
                   UNION ALL SELECT job_id FROM assessments WHERE run_id = :id) r ON r.job_id = j.id
               GROUP BY j.source""", {"id": run["id"]}).fetchall())
        if counts:
            n = sum(counts.values())
            for source, count in counts.items():
                add(source, run["step"], run["cost_usd"] * count / n)
        elif run["step"] != "ingest" and (first_other is None or run["started_at"] < first_other):
            add("seek", run["step"], run["cost_usd"])
        else:
            add("unknown", run["step"], run["cost_usd"])

    # Jobs each site brought in, and how many the latest triage kept, to show cost per useful job.
    latest = conn.execute("SELECT prompt_version FROM evaluations ORDER BY evaluated_at DESC LIMIT 1").fetchone()
    jobs = conn.execute(
        """SELECT j.source, COUNT(*) AS jobs, SUM(j.duplicate_of IS NOT NULL) AS duplicates,
                  SUM(e.decision = 'keep') AS kept
           FROM jobs j LEFT JOIN evaluations e ON e.job_id = j.id AND e.prompt_version = ?
           GROUP BY j.source""", (latest[0] if latest else None,)).fetchall()
    for row in jobs:
        totals.setdefault(row["source"], {"source": row["source"], "cost_usd": 0.0, "by_step": {}})
    counts = {row["source"]: row for row in jobs}

    order = ["seek", "linkedin"]
    result = []
    for source in sorted(totals, key=lambda s: (s == "unknown", order.index(s) if s in order else 99, s)):
        t = totals[source]
        row = counts.get(source)
        result.append({
            **t,
            "label": SOURCE_LABELS.get(source, source),
            "by_step": [{"step": k, "label": STEPS.get(k, k), "cost_usd": t["by_step"][k]}
                        for k in STEPS if k in t["by_step"]],
            "jobs": row["jobs"] if row else 0,
            "duplicates": (row["duplicates"] or 0) if row else 0,
            "kept": (row["kept"] or 0) if row else 0,
        })
    return result


def unlinked(conn):
    """Paid results not yet linked to a run (saved before runs were recorded)."""
    return sum(conn.execute(f"SELECT COUNT(*) FROM {t} WHERE run_id IS NULL AND cost_usd IS NOT NULL").fetchone()[0]
               for t, *_ in LOCAL_SOURCES)


def money(usd):
    if usd is None:
        return "-"
    return f"${usd:.2f}" if usd >= 1 else f"${usd:.4f}"


def print_summary(conn, recent):
    s = summary(conn, recent)
    print(f"Total spent: {money(s['total_usd'])}   this month ({s['month']}): {money(s['this_month_usd'])}\n")
    print("By step:")
    for row in s["by_step"]:
        items = f"{row['items']:>5} items, " if row["items"] else " " * 13
        print(f"  {row['label']:<28} {money(row['cost_usd']):>9}   {row['runs']:>3} runs, {items}last {row['last_run'][:10]}")
    print("\nBy month:")
    for m in s["by_month"]:
        parts = ", ".join(f"{STEPS[k].split(' · ')[0].lower()} {money(m['by_step'][k])}"
                          for k in STEPS if k in m["by_step"])
        print(f"  {m['month']}  {money(m['cost_usd']):>9}   ({parts})")
    print("\nBy source (scraping, plus the AI steps run on its jobs):")
    for src in s["by_source"]:
        jobs = f"{src['jobs']} jobs, {src['kept']} kept" if src["source"] != "unknown" else ""
        per = f", {money(src['cost_usd'] / src['kept'])} per kept job" if src["kept"] else ""
        print(f"  {src['label']:<22} {money(src['cost_usd']):>9}   {jobs}{per}")
        for step in src["by_step"]:
            print(f"      {step['label']:<28} {money(step['cost_usd']):>9}")
    print(f"\nLatest {len(s['recent'])} runs:")
    print(f"  {'started':<16}  {'step':<26} {'version':<15} {'items':>9} {'estimate':>9} {'actual':>9}  status")
    for r in s["recent"]:
        items = (f"{r['items_ok']}" if r["items_ok"] or r["items_failed"] else "-") + \
            (f"+{r['items_failed']}err" if r["items_failed"] else "")
        print(f"  {r['started_at'][:16].replace('T', ' '):<16}  {r['label']:<26} {(r['version'] or '-'):<15} "
              f"{items:>9} {money(r['estimate_usd']):>9} {money(r['cost_usd']):>9}  {r['status']}")
    if s["unlinked"]:
        print(f"\n{s['unlinked']} paid results from before runs were recorded aren't counted yet. "
              f"Run: python spend.py --backfill")


# --- Backfill ----------------------------------------------------------------------------------

# (table, version column, time column): results that carry their own cost_usd.
LOCAL_SOURCES = [
    ("evaluations", "prompt_version", "evaluated_at"),
    ("extractions", "requirements_version", "extracted_at"),
    ("assessments", "assess_version", "assessed_at"),
]


def step_for(table, version):
    if table == "evaluations":
        return "evaluate", "anthropic"
    if table == "extractions":
        return "requirements", "anthropic"
    return ("assess_jev", "typesafe") if version.endswith("-jev") else ("assess", "anthropic")


def backfill_local(conn):
    """One run per table, version and day, from results saved before runs were recorded."""
    added = 0
    for table, vcol, tcol in LOCAL_SOURCES:
        groups = conn.execute(
            f"""SELECT {vcol} AS version, date({tcol}) AS day, MIN(model) AS model, COUNT(*) AS n,
                       SUM(cost_usd) AS cost, MIN({tcol}) AS started, MAX({tcol}) AS finished
                FROM {table} WHERE run_id IS NULL AND cost_usd IS NOT NULL
                GROUP BY {vcol}, day ORDER BY started""").fetchall()
        for g in groups:
            step, service = step_for(table, g["version"])
            run_id = conn.execute(
                """INSERT INTO runs (step, service, version, model, started_at, finished_at, status,
                       items_planned, items_ok, cost_usd)
                   VALUES (?, ?, ?, ?, ?, ?, 'backfilled', ?, ?, ?)""",
                (step, service, g["version"], g["model"], g["started"], g["finished"], g["n"], g["n"], g["cost"]),
            ).lastrowid
            conn.execute(
                f"""UPDATE {table} SET run_id = ?
                    WHERE run_id IS NULL AND cost_usd IS NOT NULL AND {vcol} = ? AND date({tcol}) = ?""",
                (run_id, g["version"], g["day"]))
            added += 1
            print(f"  {step:<13} {g['version']:<15} {g['day']}  {g['n']:>4} results  {money(g['cost'])}")
    conn.commit()
    return added


def local_time(dt):
    return dt.astimezone().replace(tzinfo=None).isoformat(timespec="seconds") if dt else None


def sync_apify(conn):
    """Add every Apify run on the account that isn't recorded yet, and refresh the cost of those
    that are (Apify can finalise a run's cost a little after it ends)."""
    from apify_client import ApifyClient
    from dotenv import load_dotenv

    load_dotenv()
    if not os.environ.get("APIFY_TOKEN"):
        print("  APIFY_TOKEN isn't set, so Apify runs were skipped.")
        return 0, 0
    client = ApifyClient(os.environ["APIFY_TOKEN"])
    names = {}  # actor ID -> "username/name"

    def actor_name(act_id):
        if act_id not in names:
            actor = client.actor(act_id).get()
            names[act_id] = f"{actor.username}/{actor.name}" if actor else act_id
        return names[act_id]

    added = updated = offset = 0
    while True:
        page = client.runs().list(offset=offset, limit=1000)
        for r in page.items:
            cost = r.usage_total_usd or 0.0
            found = conn.execute("SELECT id, cost_usd FROM runs WHERE external_id = ?", (r.id,)).fetchone()
            if found:
                if abs(found["cost_usd"] - cost) > 1e-9:
                    conn.execute("UPDATE runs SET cost_usd = ? WHERE id = ?", (cost, found["id"]))
                    updated += 1
                continue
            status = "done" if r.status == "SUCCEEDED" else "aborted" if r.finished_at else "running"
            conn.execute(
                """INSERT INTO runs (step, service, version, model, started_at, finished_at, status, cost_usd, external_id)
                   VALUES (?, 'apify', NULL, ?, ?, ?, ?, ?, ?)""",
                (db.ACTOR_STEPS.get(actor_name(r.act_id), "ingest"), actor_name(r.act_id), local_time(r.started_at), local_time(r.finished_at), status, cost, r.id))
            added += 1
            print(f"  apify  {local_time(r.started_at)}  {actor_name(r.act_id):<34} {money(cost)}")
        offset += len(page.items)
        if not page.items or offset >= page.total:
            break
    conn.commit()
    return added, updated


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=10, help="how many recent runs to list (default 10)")
    ap.add_argument("--backfill", action="store_true",
                    help="record spend saved before runs existed, and fetch Apify run costs")
    args = ap.parse_args()

    conn = db.connect()
    if args.backfill:
        print("Results saved before runs were recorded:")
        n = backfill_local(conn)
        print(f"  {n} runs added" if n else "  nothing to add")
        print("\nApify runs:")
        added, updated = sync_apify(conn)
        print(f"  {added} added, {updated} costs refreshed\n")
    print_summary(conn, args.runs)


if __name__ == "__main__":
    sys.exit(main())
