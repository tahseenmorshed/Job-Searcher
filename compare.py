"""Compare two assessment versions on the jobs they both assessed, e.g. Sonnet vs Haiku.

No AI calls: everything is read from the database and scored with today's scoring.py rules.

Usage:
  python compare.py assess-v1 assess-v1-haiku            # summary
  python compare.py assess-v1 assess-v1-haiku --job 196  # one job's criteria side by side
"""

import argparse
from collections import Counter

import checks
import db
import scoring

MATCH_ORDER = ["direct", "transferable", "side_project", "stretch", "none"]
IMPORTANCE_ORDER = ["must_have", "preferred", "bonus"]
# Two criteria are treated as the same requirement if this share of their words overlap.
MIN_OVERLAP = 0.4


def criterion_words(c):
    return {w for w in checks.words(f"{c['requirement']} {c.get('listing_quote') or ''}")
            if w not in checks.SMALL_WORDS}


def overlap(a, b):
    return len(a & b) / len(a | b) if a and b else 0.0


def pair_criteria(base, other):
    """Pair each base criterion with the most similar one in other. Returns (pairs, only_base, only_other)."""
    candidates = sorted(
        ((overlap(criterion_words(b), criterion_words(o)), i, j)
         for i, b in enumerate(base) for j, o in enumerate(other)),
        reverse=True,
    )
    used_b, used_o, pairs = set(), set(), []
    for score, i, j in candidates:
        if score < MIN_OVERLAP:
            break
        if i not in used_b and j not in used_o:
            used_b.add(i)
            used_o.add(j)
            pairs.append((base[i], other[j]))
    only_base = [c for i, c in enumerate(base) if i not in used_b]
    only_other = [c for j, c in enumerate(other) if j not in used_o]
    return pairs, only_base, only_other


def steps_apart(order, a, b):
    return abs(order.index(a) - order.index(b))


def pct(n, total):
    return f"{100 * n / total:.0f}%" if total else "-"


def print_matrix(title, order, pairs):
    """Rows: base version's value. Columns: other version's value."""
    counts = Counter(pairs)
    width = max(len(v) for v in order) + 2
    print(f"\n{title} (rows: base, columns: other)")
    print(" " * width + "".join(f"{v[:12]:>14}" for v in order))
    for row in order:
        print(f"{row:<{width}}" + "".join(f"{counts[(row, col)] or '.':>14}" for col in order))


def summary(base_v, other_v, base, other, jobs):
    shared = sorted(base.keys() & other.keys())
    if not shared:
        print(f"No jobs assessed under both {base_v} and {other_v}.")
        return
    print(f"{base_v} (base) vs {other_v} · {len(shared)} jobs assessed by both\n")

    # Cost and output.
    for v, results in ((base_v, base), (other_v, other)):
        rows = [results[j] for j in shared]
        cost = sum(a["cost_usd"] or 0 for a in rows)
        crit = sum(len(a["criteria"]) for a in rows) / len(rows)
        flagged = sum(1 for a in rows if a["flag_count"])
        flags = sum(a["flag_count"] for a in rows)
        print(f"  {v:<22} cost ${cost:.3f} (${cost / len(rows):.4f}/job) · {crit:.1f} criteria/job · "
              f"{flags} quote flags on {flagged} jobs")

    # Final ratings and scores.
    rating_pairs = [(base[j]["rating"], other[j]["rating"]) for j in shared]
    same = sum(1 for a, b in rating_pairs if a == b)
    one = sum(1 for a, b in rating_pairs if steps_apart(scoring.RATINGS, a, b) == 1)
    diffs = [other[j]["score"] - base[j]["score"] for j in shared]
    print(f"\nRatings: same {same} ({pct(same, len(shared))}) · one step apart {one} ({pct(one, len(shared))}) · "
          f"further apart {len(shared) - same - one} ({pct(len(shared) - same - one, len(shared))})")
    print(f"Scores: average difference {sum(abs(d) for d in diffs) / len(diffs):.1f} points · "
          f"{other_v} is {sum(diffs) / len(diffs):+.1f} points on average · "
          f"within 10 points: {pct(sum(1 for d in diffs if abs(d) <= 10), len(diffs))}")
    top = max(1, len(shared) // 5)
    base_top = set(sorted(shared, key=lambda j: -base[j]["score"])[:top])
    other_top = set(sorted(shared, key=lambda j: -other[j]["score"])[:top])
    print(f"Top {top} jobs by score: {len(base_top & other_top)} in common")
    print_matrix("Final rating", scoring.RATINGS, rating_pairs)

    # Per-criterion agreement.
    all_pairs, only_b, only_o = [], 0, 0
    for j in shared:
        pairs, ob, oo = pair_criteria(base[j]["criteria"], other[j]["criteria"])
        all_pairs += pairs
        only_b += len(ob)
        only_o += len(oo)
    n = len(all_pairs)
    match_same = sum(1 for b, o in all_pairs if b["match"] == o["match"])
    match_near = sum(1 for b, o in all_pairs if steps_apart(MATCH_ORDER, b["match"], o["match"]) <= 1)
    imp_same = sum(1 for b, o in all_pairs if b["importance"] == o["importance"])
    generous = sum(1 for b, o in all_pairs if MATCH_ORDER.index(o["match"]) < MATCH_ORDER.index(b["match"]))
    harsher = sum(1 for b, o in all_pairs if MATCH_ORDER.index(o["match"]) > MATCH_ORDER.index(b["match"]))
    print(f"\nCriteria: {n} paired · {only_b} found only by {base_v} · {only_o} found only by {other_v}")
    print(f"  Match: same {pct(match_same, n)} · within one step {pct(match_near, n)} · "
          f"{other_v} more generous on {generous}, harsher on {harsher}")
    print(f"  Importance: same {pct(imp_same, n)}")
    print_matrix("Match", MATCH_ORDER, [(b["match"], o["match"]) for b, o in all_pairs])
    print_matrix("Importance", IMPORTANCE_ORDER, [(b["importance"], o["importance"]) for b, o in all_pairs])

    # Biggest movers.
    print("\nBiggest score changes")
    for j in sorted(shared, key=lambda j: -abs(other[j]["score"] - base[j]["score"]))[:10]:
        b, o = base[j], other[j]
        print(f"  {b['score']:>3} -> {o['score']:>3}  {scoring.LABELS[b['rating']]} -> {scoring.LABELS[o['rating']]}"
              f"  · {jobs[j]['title']} · {jobs[j]['company']} (job {j})")
    print(f"\nSee one job side by side: python compare.py {base_v} {other_v} --job <id>")


def side_by_side(base_v, other_v, a, b, job):
    print(f"{job['title']} · {job['company']} (job {job['id']})")
    print(f"  {base_v}: {a['score']} {scoring.LABELS[a['rating']]} · {other_v}: {b['score']} {scoring.LABELS[b['rating']]}\n")
    pairs, only_a, only_b = pair_criteria(a["criteria"], b["criteria"])
    fmt = "  {:<38} {:<26} {:<26}"
    print(fmt.format("Requirement", base_v, other_v))
    for x, y in pairs:
        mark = "" if (x["match"], x["importance"]) == (y["match"], y["importance"]) else "  <-"
        print(fmt.format(x["requirement"][:38], f"{x['importance']}/{x['match']}",
                         f"{y['importance']}/{y['match']}") + mark)
    for x in only_a:
        print(fmt.format(x["requirement"][:38], f"{x['importance']}/{x['match']}", "(not found)"))
    for y in only_b:
        print(fmt.format(y["requirement"][:38], "(not found)", f"{y['importance']}/{y['match']}"))
    for v, r in ((base_v, a), (other_v, b)):
        print(f"\n  {v}: {r['overall_reason']}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("base", help="the version to compare against, e.g. assess-v1")
    ap.add_argument("other", help="the version being tested, e.g. assess-v1-haiku")
    ap.add_argument("--job", type=int, help="show one job's criteria side by side")
    args = ap.parse_args()

    conn = db.connect()
    base = scoring.rated_assessments(conn, args.base)
    other = scoring.rated_assessments(conn, args.other)
    jobs = {row["id"]: dict(row) for row in conn.execute("SELECT id, title, company FROM jobs")}
    if args.job:
        if args.job not in base or args.job not in other:
            print(f"Job {args.job} isn't assessed under both versions.")
            return
        side_by_side(args.base, args.other, base[args.job], other[args.job], jobs[args.job])
    else:
        summary(args.base, args.other, base, other, jobs)


if __name__ == "__main__":
    main()
