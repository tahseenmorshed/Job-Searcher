"""Turn an assessment's per-criterion ratings into a score and a rating.

This runs whenever results are shown (assess.py --show, the API), not when they're saved,
so changing a weight or threshold here re-rates every saved assessment with no new AI calls.
"""

import json
import tomllib
from pathlib import Path

import checks

# How much the employer cares about a criterion.
IMPORTANCE_WEIGHT = {"must_have": 3, "preferred": 2, "bonus": 1}
# Boilerplate like "strong communicator" counts for a quarter, so it can't inflate the score.
GENERIC_FACTOR = 0.25

# How well the resume covers a criterion.
MATCH_SCORE = {"direct": 1.0, "transferable": 0.75, "side_project": 0.5, "stretch": 0.25, "none": 0.0}

# Minimum score (0-100) for each rating, best first. Anything lower is not_suitable.
# First guesses: tune these after comparing with your own ratings.
THRESHOLDS = [(85, "very_strong"), (70, "strong"), (55, "match"), (35, "stretch")]
RATINGS = ["very_strong", "strong", "match", "stretch", "not_suitable"]  # best first
LABELS = {"very_strong": "Very strong match", "strong": "Strong match", "match": "Match",
          "stretch": "Stretch", "not_suitable": "Not suitable"}

_candidate = tomllib.loads(Path("preferences.toml").read_text()).get("candidate", {})
HAS_CLEARANCE = _candidate.get("security_clearance", "none").lower() != "none"


def criterion_score(c):
    """0-1 for one criterion. When the listing accepts equivalents ("or similar"), a close
    equivalent counts as fully meeting it."""
    if c["flexible"] and c["match"] == "transferable":
        return 1.0
    return MATCH_SCORE[c["match"]]


def criterion_weight(c):
    return IMPORTANCE_WEIGHT[c["importance"]] * (GENERIC_FACTOR if c["generic"] else 1)


def lower(rating, cap):
    """The worse of two ratings."""
    return max(rating, cap, key=RATINGS.index)


def rate(criteria, clearance_requirement):
    """Return {"score", "rating", "caps"}. caps lists the rules that held the rating down."""
    total = sum(criterion_weight(c) for c in criteria)
    score = round(100 * sum(criterion_weight(c) * criterion_score(c) for c in criteria) / total) if total else 0
    rating = next((name for minimum, name in THRESHOLDS if score >= minimum), "not_suitable")

    caps = []
    for c in criteria:
        if c["importance"] != "must_have" or c["generic"]:
            continue
        if c["match"] == "none":
            rating = lower(rating, "stretch")
            caps.append(f"Missing must-have: {c['requirement']}")
        elif c["match"] == "stretch":
            rating = lower(rating, "match")
            caps.append(f"Big gap on must-have: {c['requirement']}")
    if clearance_requirement == "must_already_hold" and not HAS_CLEARANCE:
        rating = "not_suitable"
        caps.append("Needs an existing security clearance")
    return {"score": score, "rating": rating, "caps": caps}


def latest_version(conn):
    row = conn.execute("SELECT assess_version FROM assessments ORDER BY assessed_at DESC LIMIT 1").fetchone()
    return row[0] if row else None


def rated_assessments(conn, version):
    """{job_id: assessment} for one assess version, each with its criteria, score, rating and caps."""
    resumes = {}  # prompt_hash -> resume text the model saw
    found = {}
    rows = conn.execute(
        """SELECT a.*, j.title, j.company, j.location, j.work_arrangement, j.employment_type, j.description
           FROM assessments a JOIN jobs j ON j.id = a.job_id WHERE a.assess_version = ?""",
        (version,),
    )
    for row in rows:
        a = dict(row)
        for key in ("strengths", "gaps", "lead_with"):
            a[key] = json.loads(a[key] or "[]")
        a["criteria"] = [
            {**dict(c), "generic": bool(c["generic"]), "flexible": bool(c["flexible"]),
             "resume_quotes": json.loads(c["resume_quotes"] or "[]")}
            for c in conn.execute(
                "SELECT * FROM assessment_criteria WHERE assessment_id = ? ORDER BY position", (a["id"],))
        ]
        for c in a["criteria"]:
            c["weight"] = criterion_weight(c)
            c["score"] = criterion_score(c)
        a.update(rate(a["criteria"], a["clearance_requirement"]))

        if a["prompt_hash"] not in resumes:
            saved = conn.execute("SELECT system_text FROM prompt_versions WHERE prompt_hash = ?",
                                 (a["prompt_hash"],)).fetchone()
            resumes[a["prompt_hash"]] = checks.resume_from_system(saved[0] if saved else "")
        # The listing as the model saw it (same fields as assess.listing_message).
        listing = "\n".join(a.pop(k) or "" for k in
                            ("title", "company", "location", "work_arrangement", "employment_type", "description"))
        checks.check_assessment(a, listing, resumes[a["prompt_hash"]])
        found[a["job_id"]] = a
    return found
