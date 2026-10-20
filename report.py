"""Show triage results from data/jobseeker.db: kept jobs to look at, rejected jobs to sanity-check.

Usage:
  python report.py                       # latest prompt version, kept and rejected
  python report.py --kept                # kept jobs only
  python report.py --rejected            # rejected jobs only
  python report.py --new                 # only jobs first seen in the last 24 hours
  python report.py --only core           # one group: core, adjacent, open_background,
                                         #   too_much_experience, not_relevant, title_excluded
  python report.py --version triage-v1   # a specific prompt version
  python report.py --compare triage-v1   # jobs whose result changed between triage-v1 and the current version
"""

import argparse
from datetime import datetime, timedelta

import db

KEPT_GROUPS = [("core", "CORE"), ("adjacent", "ADJACENT"), ("open_background", "OPEN TO SOFTWARE BACKGROUND")]
REJECTED_GROUPS = [("too_much_experience", "TOO MUCH EXPERIENCE"), ("not_relevant", "NOT RELEVANT"),
                   ("title_excluded", "TITLE EXCLUDED")]


def posted(value):
    """'2026-10-16T20:00:18.935Z' -> '16 Oct'."""
    try:
        return datetime.fromisoformat(value).strftime("%d %b").lstrip("0")
    except (TypeError, ValueError):
        return "date unknown"


def results(conn, version, since=None):
    """One row per job: the job's details joined with its result for this prompt version."""
    sql = """SELECT j.id, j.title, j.company, j.location, j.work_arrangement, j.salary_text, j.posted_at,
                    j.url, j.first_seen_at, e.decision, e.reason, e.relevance, e.relevance_reason,
                    e.min_years_required, e.years_evidence, e.open_background_evidence, e.title_term
             FROM evaluations e JOIN jobs j ON j.id = e.job_id
             WHERE e.prompt_version = ? AND j.duplicate_of IS NULL"""
    params = [version]
    if since:
        sql += " AND j.first_seen_at >= ?"
        params.append(since)
    sql += " ORDER BY j.posted_at DESC"
    return [dict(row) for row in conn.execute(sql, params)]


def print_kept(rows, only=None):
    for reason, heading in KEPT_GROUPS:
        if only and reason != only:
            continue
        group = [r for r in rows if r["decision"] == "keep" and r["reason"] == reason]
        print(f"\n{heading} ({len(group)})")
        for r in group:
            details = [r["company"], r["location"], r["work_arrangement"], r["salary_text"],
                       f"posted {posted(r['posted_at'])}"]
            if r["min_years_required"] is not None:
                details.append(f"{r['min_years_required']}+ yrs")
            print(f"  {r['title']} · " + " · ".join(d for d in details if d))
            print(f"    {r['open_background_evidence'] or r['relevance_reason']}")
            print(f"    {r['url']}")


def print_rejected(rows, only=None):
    for reason, heading in REJECTED_GROUPS:
        if only and reason != only:
            continue
        group = [r for r in rows if r["decision"] == "reject" and r["reason"] == reason]
        print(f"\n{heading} ({len(group)})")
        for r in group:
            if reason == "too_much_experience":
                why = f"{r['min_years_required']} yrs · {r['years_evidence']}"
            elif reason == "title_excluded":
                why = f"[{r['title_term']}]"
            else:
                why = r["relevance_reason"]
            print(f"  {r['title']} · {r['company']} · {why}")


def print_compare(conn, old, new):
    before = {r["id"]: r for r in results(conn, old)}
    after = {r["id"]: r for r in results(conn, new)}
    both = before.keys() & after.keys()
    changed = [(before[i], after[i]) for i in both
               if (before[i]["decision"], before[i]["reason"]) != (after[i]["decision"], after[i]["reason"])]
    for version in (old, new):
        notes = [row[0] for row in conn.execute(
            "SELECT notes FROM prompt_versions WHERE prompt_version = ? ORDER BY first_used_at", (version,))]
        print(f"{version}: {' / '.join(n for n in notes if n) or 'no notes recorded'}")
    print(f"\n{old} -> {new}: {len(both)} jobs have results in both, {len(changed)} changed")
    for label, test in [
        ("NOW KEPT (were rejected)", lambda b, a: b["decision"] == "reject" and a["decision"] == "keep"),
        ("NOW REJECTED (were kept)", lambda b, a: b["decision"] == "keep" and a["decision"] == "reject"),
        ("MOVED WITHIN KEPT OR REJECTED", lambda b, a: b["decision"] == a["decision"]),
    ]:
        group = [(b, a) for b, a in changed if test(b, a)]
        print(f"\n{label} ({len(group)})")
        for b, a in sorted(group, key=lambda pair: pair[1]["title"]):
            print(f"  {a['title']} · {a['company']} · {b['reason']} -> {a['reason']}")
            print(f"    {a['relevance_reason'] or ''}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--version", help="prompt version to show (default: the most recently used)")
    ap.add_argument("--kept", action="store_true", help="only kept jobs")
    ap.add_argument("--rejected", action="store_true", help="only rejected jobs")
    ap.add_argument("--new", action="store_true", help="only jobs first seen in the last 24 hours")
    ap.add_argument("--only", choices=[key for key, _ in KEPT_GROUPS + REJECTED_GROUPS],
                    help="show a single group, e.g. core")
    ap.add_argument("--compare", metavar="OLD_VERSION", help="show jobs whose result changed since OLD_VERSION")
    args = ap.parse_args()

    conn = db.connect()
    version = args.version or (conn.execute(
        "SELECT prompt_version FROM evaluations ORDER BY evaluated_at DESC LIMIT 1").fetchone() or [None])[0]
    if not version:
        print("No triage results yet. Run evaluate.py first.")
        return

    if args.compare:
        print_compare(conn, args.compare, version)
        return

    since = (datetime.now() - timedelta(days=1)).isoformat(timespec="seconds") if args.new else None
    rows = results(conn, version, since)
    kept = [r for r in rows if r["decision"] == "keep"]
    untriaged = conn.execute(
        """SELECT COUNT(*) FROM jobs j WHERE j.duplicate_of IS NULL AND NOT EXISTS
               (SELECT 1 FROM evaluations e WHERE e.job_id = j.id AND e.prompt_version = ?)""",
        (version,),
    ).fetchone()[0]

    counts = ", ".join(f"{label.lower()} {sum(1 for r in kept if r['reason'] == key)}"
                       for key, label in KEPT_GROUPS)
    print(f"{version} · {len(rows)} jobs{' first seen in the last 24h' if args.new else ''} · "
          f"kept {len(kept)} ({counts}) · rejected {len(rows) - len(kept)} · "
          f"not yet triaged {untriaged}")
    if untriaged:
        print("Run evaluate.py to triage the rest.")

    kept_keys = {key for key, _ in KEPT_GROUPS}
    if not args.rejected and (not args.only or args.only in kept_keys):
        print_kept(rows, args.only)
    if not args.kept and (not args.only or args.only not in kept_keys):
        print_rejected(rows, args.only)


if __name__ == "__main__":
    main()
