"""Find listings that are the same job, within one site or across Seek and LinkedIn.

Every listing is kept. A duplicate gets jobs.duplicate_of pointing at the copy that's kept, which
hides it from the results and from every paid step (triage, requirements, Jev). Runs on its own
after each ingest; free, no API calls.

How two listings are compared:
  1. First check: same city, posted within MAX_DAYS_APART of each other, and the same title or
     the same company once both are cleaned up (case, punctuation, "Pty Ltd" and so on).
  2. Description similarity, from 0 to 1, comparing the words in order.
  3. Duplicate if the titles match and similarity >= DUPLICATE_SIMILARITY. Listings with different
     titles are never hidden, however similar: a team often posts several roles from one template
     (e.g. Senior Android Engineer and Senior iOS Engineer at 99%). Pairs at POSSIBLE_SIMILARITY
     or above are recorded as possible duplicates, for review, without hiding anything.

Usage:
  python dedupe.py           # check every listing and update duplicate_of
  python dedupe.py --show    # list the matched pairs, without changing anything
"""

import argparse
import re
import sys
from datetime import datetime
from difflib import SequenceMatcher

import db

DUPLICATE_SIMILARITY = 0.95  # same title and at least this similar: hidden as a duplicate
POSSIBLE_SIMILARITY = 0.80   # at least this similar otherwise: recorded for review only
MAX_DAYS_APART = 14          # listings posted further apart than this are never compared

# Words dropped from company names before comparing ("Halcyon Knights Pty Ltd" = "Halcyon Knights").
COMPANY_FILLER = re.compile(r"\b(pty|ltd|limited|australia|australian|group|inc|co|corporation)\b")


def clean(text):
    return " ".join(re.sub(r"[^a-z0-9 ]", " ", (text or "").lower()).split())


def clean_company(name):
    # "WA Primary Health Alliance - WAPHA": the part after a dash is usually an abbreviation.
    name = (name or "").split(" - ")[0]
    return " ".join(COMPANY_FILLER.sub(" ", clean(name)).split())


def city(location):
    # Both sites store 'Suburb, City STATE' or 'City STATE' (LinkedIn's are rewritten by db.py).
    return (location or "").split(",")[-1].strip()


def words(text):
    return re.findall(r"[a-z0-9]+", (text or "").lower())


def similarity(a, b):
    """How much of the two descriptions is the same words in the same order, from 0 to 1."""
    if not a or not b:
        return 0.0
    matcher = SequenceMatcher(None, a, b, autojunk=False)
    if matcher.real_quick_ratio() < POSSIBLE_SIMILARITY or matcher.quick_ratio() < POSSIBLE_SIMILARITY:
        return 0.0  # can't reach the lower threshold, so skip the slow comparison
    return matcher.ratio()


def posted(job):
    try:
        return datetime.fromisoformat((job["posted_at"] or "").replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def close_in_time(a, b):
    pa, pb = posted(a), posted(b)
    return pa is None or pb is None or abs((pa - pb).days) <= MAX_DAYS_APART


def find_pairs(jobs):
    """Every pair passing the first check, with its similarity. Yields (a, b, similarity, same_title)."""
    by_key = {}  # (city, 'title' or 'company', cleaned value) -> jobs
    for job in jobs:
        c = city(job["location"])
        for kind, value in (("title", clean(job["title"])), ("company", clean_company(job["company"]))):
            if value:
                by_key.setdefault((c, kind, value), []).append(job)
    seen = set()
    for group in by_key.values():
        for i, a in enumerate(group):
            for b in group[i + 1:]:
                pair = (min(a["id"], b["id"]), max(a["id"], b["id"]))
                if pair in seen or not close_in_time(a, b):
                    continue
                seen.add(pair)
                score = similarity(a["words"], b["words"])
                if score >= POSSIBLE_SIMILARITY:
                    yield a, b, score, clean(a["title"]) == clean(b["title"])


def keeper_rank(job):
    """Which copy of a duplicate is kept: one already assessed, then triaged, then the first seen."""
    return (not job["assessed"], not job["triaged"], job["first_seen_at"], job["id"])


def check(conn, quiet=False):
    """Rebuild job_matches and duplicate_of from scratch. Pairs you've reviewed keep your verdict.
    Returns (duplicates, possible)."""
    jobs = [dict(row) for row in conn.execute(
        """SELECT j.id, j.source, j.title, j.company, j.location, j.posted_at, j.first_seen_at, j.description,
                  EXISTS (SELECT 1 FROM evaluations e WHERE e.job_id = j.id) AS triaged,
                  EXISTS (SELECT 1 FROM assessments a WHERE a.job_id = j.id) AS assessed
           FROM jobs j""")]
    for job in jobs:
        job["words"] = words(job["description"])
    by_id = {job["id"]: job for job in jobs}

    reviewed = {(r["job_id"], r["other_job_id"]): r["review"] for r in conn.execute(
        "SELECT job_id, other_job_id, review FROM job_matches WHERE review IS NOT NULL")}
    now = datetime.now().isoformat(timespec="seconds")
    found = {}
    for a, b, score, same_title in find_pairs(jobs):
        pair = (min(a["id"], b["id"]), max(a["id"], b["id"]))
        status = "duplicate" if same_title and score >= DUPLICATE_SIMILARITY else "possible"
        found[pair] = (score, same_title, status)

    conn.execute("DELETE FROM job_matches WHERE review IS NULL")
    conn.executemany(
        """INSERT INTO job_matches (job_id, other_job_id, similarity, same_title, status, checked_at)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT (job_id, other_job_id) DO UPDATE SET
               similarity = excluded.similarity, same_title = excluded.same_title,
               status = excluded.status, checked_at = excluded.checked_at""",
        [(a, b, score, int(same), status, now) for (a, b), (score, same, status) in found.items()])

    # Group listings that are copies of each other (A = B and B = C makes one group), using the
    # check's verdict unless you've reviewed the pair.
    parent = {}

    def root(x):
        while parent.get(x, x) != x:
            x = parent[x]
        return x

    pairs = {pair: status for pair, (_, _, status) in found.items()}
    for pair, verdict in reviewed.items():
        pairs[pair] = "duplicate" if verdict == "same" else "different"
    for (a, b), status in pairs.items():
        if status == "duplicate" and a in by_id and b in by_id:
            parent[root(a)] = root(b)

    groups = {}
    for job_id in by_id:
        groups.setdefault(root(job_id), []).append(by_id[job_id])
    duplicate_of = {}
    for group in groups.values():
        if len(group) > 1:
            keep = min(group, key=keeper_rank)
            for job in group:
                if job is not keep:
                    duplicate_of[job["id"]] = keep["id"]

    conn.execute("UPDATE jobs SET duplicate_of = NULL WHERE duplicate_of IS NOT NULL")
    conn.executemany("UPDATE jobs SET duplicate_of = ? WHERE id = ?",
                     [(keep, job_id) for job_id, keep in duplicate_of.items()])
    conn.commit()

    possible = sum(1 for _, _, status in found.values() if status == "possible")
    if not quiet:
        print(f"Duplicates: {len(duplicate_of)} listings hidden, {possible} possible duplicates to review "
              f"(python dedupe.py --show)")
    return len(duplicate_of), possible


def show(conn):
    rows = conn.execute(
        """SELECT m.*, a.source AS a_source, a.title AS a_title, a.company AS a_company, a.duplicate_of AS a_dup,
                  b.source AS b_source, b.title AS b_title, b.company AS b_company, b.duplicate_of AS b_dup,
                  b.location AS location
           FROM job_matches m JOIN jobs a ON a.id = m.job_id JOIN jobs b ON b.id = m.other_job_id
           ORDER BY m.status, m.similarity DESC""").fetchall()
    if not rows:
        print("No matches. Run: python dedupe.py")
        return
    for status, heading in (("duplicate", "Duplicates (one copy hidden)"),
                            ("possible", "Possible duplicates (both kept; titles differ or less similar)")):
        group = [r for r in rows if r["status"] == status]
        print(f"\n{heading} ({len(group)})")
        for r in group:
            review = f"  [reviewed: {r['review']}]" if r["review"] else ""
            print(f"  {r['similarity']:.0%}  {city(r['location'])}{review}")
            for side in ("a", "b"):
                hidden = " (hidden)" if r[f"{side}_dup"] else ""
                job_id = r["job_id"] if side == "a" else r["other_job_id"]
                print(f"        {job_id:>5} {r[f'{side}_source']:<8} {r[f'{side}_title']} · {r[f'{side}_company']}{hidden}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--show", action="store_true", help="list matched pairs without checking again")
    args = ap.parse_args()
    conn = db.connect()
    if not args.show:
        check(conn)
    show(conn)


if __name__ == "__main__":
    sys.exit(main())
