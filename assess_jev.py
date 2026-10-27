"""Rate each core job's requirements with Jev (TypeSafe AI).

The requirements come from requirements.py, where Haiku lists them from the listing. Jev then
answers two requests per job:
  1. reading the listing: how essential each requirement is, whether equivalents are accepted,
     and the clearance requirement
  2. reading the resume: how well the candidate covers each requirement

Every request is saved exactly as sent (jev_requests table), and results are saved as their own
version, so scoring.py, the web UI and compare.py work on them unchanged.

Versions:
  assess-v1-jev  rated Sonnet's requirement lists; the match question included the listing sentence
  assess-v2-jev  rates Haiku's lists (requirements.py); the match question names the requirement only

Usage:
  python assess_jev.py                      # every job with a requirement list and no result yet
  python assess_jev.py --limit 5            # the first 5
  python assess_jev.py --job 34             # specific jobs
  python assess_jev.py --job 34 --dry       # print every question for a job, no API call
  python assess_jev.py --show-requests 34   # exactly what Jev was asked for job 34, with its answers
"""

import argparse
import hashlib
import inspect
import json
import os
import sys
import tomllib
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from typesafe_sdk import Choice, Noul, Score, TypeSafeAPIError, TypeSafeClient, TypeSafeError

import db

REQUIREMENTS_VERSION = "reqs-v1"  # which requirements.py lists to rate
VERSION = "assess-v2-jev"
PRICE_PER_M_INPUT = 0.042         # USD per million input tokens; output is free
RESUME_PATH = Path("resume.md")

# Ordered lowest first (Score questions are ordered scales). The keys are the values saved
# to the database, matching the Sonnet assessment's labels.
IMPORTANCE_LEVELS = {
    "bonus": "Nice to have: described as a bonus, an advantage, exposure or familiarity",
    "preferred": "Preferred: described as ideal, desirable, preferable or highly regarded, but not essential",
    "must_have": "Required: stated as required, essential or must-have, listed under requirements with no "
                 "softer wording, or plainly the core of the day-to-day work",
}
MATCH_LEVELS = {
    "none": "None: nothing in the resume relates to this requirement",
    "stretch": "Stretch: related experience, but a real gap the candidate would need to learn",
    "side_project": "Side project: the candidate has done this, but only in a side project, study or briefly",
    "transferable": "Transferable: professional experience with a close equivalent that would carry over "
                    "within weeks, e.g. C# for Java",
    "direct": "Direct: the candidate has done this in a professional role, at the level asked",
}
CLEARANCE_OPTIONS = {
    "none": "No mention of security clearance or citizenship",
    "citizenship_only": "Must be a citizen, with no clearance needed",
    "obtainable": "Must hold or be able to obtain a security clearance",
    "must_already_hold": "Must already hold a security clearance",
}


def listing_state(job):
    return {
        "title": job["title"],
        "company": job.get("company") or "unknown",
        "location": job.get("location") or "unknown",
        "work_arrangement": job.get("work_arrangement") or "unknown",
        "employment_type": job.get("employment_type") or "unknown",
        "description": job.get("description") or "",
    }


def resume_state(resume, prefs):
    candidate = prefs.get("candidate", {})
    return {
        "candidate_facts": {
            "years_of_professional_experience": prefs.get("years_experience"),
            "work_rights": candidate.get("work_rights"),
            "current_security_clearance": candidate.get("security_clearance"),
        },
        "resume": resume,
    }


def requirement_text(c):
    quote = f' (the listing says: "{c["listing_quote"]}")' if c.get("listing_quote") else ""
    return f"{c['requirement']}{quote}"


def listing_questions(criteria):
    questions = {}
    for i, c in enumerate(criteria):
        req = requirement_text(c)
        questions[f"importance_{i}"] = Score(
            instructions=f"How strongly this listing requires: {req}",
            criteria=list(IMPORTANCE_LEVELS.values()),
        )
        questions[f"flexible_{i}"] = Noul(
            instructions=f"The listing accepts equivalents for this requirement, for example 'or similar' or "
                         f"'or equivalent': {req}",
        )
    questions["clearance"] = Choice(
        instructions="What the listing requires about security clearance or citizenship",
        criteria=CLEARANCE_OPTIONS,
    )
    return questions


def resume_questions(criteria):
    # Only the requirement's name. The listing sentence can name other skills ("SQL and MongoDB"),
    # which would then count against this requirement as well as their own.
    return {
        f"match_{i}": Score(
            instructions=f"How well the candidate's resume covers this job requirement: {c['requirement']}. "
                         "Judge from what the resume shows, not from what the candidate could probably learn.",
            criteria=list(MATCH_LEVELS.values()),
        )
        for i, c in enumerate(criteria)
    }


def top_level(answer, labels):
    """The most likely level of a Score answer, as our label."""
    best = max(answer.probabilities, key=answer.probabilities.get)
    return list(labels)[int(best)]


def estimate_tokens(job, criteria, resume):
    """Rough count (~4 characters per token) for the pre-run estimate only."""
    chars = len(json.dumps(listing_state(job))) + len(resume)
    chars += len(criteria) * (sum(map(len, IMPORTANCE_LEVELS.values())) + sum(map(len, MATCH_LEVELS.values())) + 450)
    chars += sum(map(len, CLEARANCE_OPTIONS.values())) + 100
    return chars // 4


def load_jobs(conn, args, include_done=False):
    """Jobs with a REQUIREMENTS_VERSION list (and, unless include_done, no VERSION result yet), each with its criteria."""
    sql = """SELECT j.*, x.id AS extraction_id, x.triage_version FROM jobs j
             JOIN extractions x ON x.job_id = j.id AND x.requirements_version = ?
             WHERE (? OR NOT EXISTS (SELECT 1 FROM assessments a WHERE a.job_id = j.id AND a.assess_version = ?))"""
    params = [REQUIREMENTS_VERSION, include_done, VERSION]
    if args.job:
        sql += f" AND j.id IN ({','.join('?' * len(args.job))})"
        params += args.job
    sql += " ORDER BY j.posted_at DESC"
    jobs = [dict(row) for row in conn.execute(sql, params)][: args.limit]
    for job in jobs:
        job["criteria"] = [dict(c) for c in conn.execute(
            "SELECT * FROM requirements WHERE extraction_id = ? ORDER BY position", (job["extraction_id"],))]
    return jobs


def fingerprint(resume, prefs):
    """A short hash of everything that shapes Jev's answers apart from the listing: the resume,
    the candidate facts, and the question wording. Saved with each result, so a run can warn when
    any of it changed without VERSION being bumped."""
    parts = [json.dumps(resume_state(resume, prefs), sort_keys=True),
             json.dumps([IMPORTANCE_LEVELS, MATCH_LEVELS, CLEARANCE_OPTIONS]),
             *(inspect.getsource(f) for f in (requirement_text, listing_questions, resume_questions))]
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()[:12]


def build_requests(job, resume, prefs):
    """The two requests for one job: [(state, questions), (state, questions)]."""
    criteria = job["criteria"]
    return [(listing_state(job), listing_questions(criteria)),
            (resume_state(resume, prefs), resume_questions(criteria))]


def assess(client, job, resume, prefs):
    requests = build_requests(job, resume, prefs)
    responses = [client.system_one(state=state, questions=questions) for state, questions in requests]
    tokens = sum(r.usage.input_tokens or 0 for r in responses)
    return requests, responses, tokens


def save(conn, job, requests, responses, tokens, prompt_hash):
    r1, r2 = responses
    clearance = r1.answers["clearance"]
    cur = conn.execute(
        """INSERT INTO assessments (job_id, assess_version, prompt_hash, model, triage_version,
               clearance_evidence, clearance_requirement, listing_detail, strengths, gaps, lead_with,
               overall_reason, model_rating, cost_usd, assessed_at, requirements_version)
           VALUES (?, ?, ?, ?, ?, NULL, ?, NULL, '[]', '[]', '[]', ?, NULL, ?, ?, ?)""",
        (job["id"], VERSION, prompt_hash, r1.model, job["triage_version"], clearance.choice,
         f"Rated by {r1.model}; requirements listed by Haiku ({REQUIREMENTS_VERSION}).",
         tokens * PRICE_PER_M_INPUT / 1_000_000, datetime.now().isoformat(timespec="seconds"), REQUIREMENTS_VERSION),
    )
    rows = []
    for i, c in enumerate(job["criteria"]):
        imp, flex, match = r1.answers[f"importance_{i}"], r1.answers[f"flexible_{i}"], r2.answers[f"match_{i}"]
        jev = {"importance_probabilities": imp.probabilities, "importance_confidence": imp.confidence,
               "flexible_probability": flex.noul,
               "match_probabilities": match.probabilities, "match_confidence": match.confidence,
               "haiku_importance": c.get("haiku_importance")}
        rows.append((cur.lastrowid, i, c["requirement"], c["listing_quote"], c["generic"], None,
                     top_level(imp, IMPORTANCE_LEVELS), flex.noul >= 0.5, "[]", None, None,
                     top_level(match, MATCH_LEVELS), json.dumps(jev)))
    conn.executemany(
        """INSERT INTO assessment_criteria (assessment_id, position, requirement, listing_quote, generic,
               importance_reason, importance, flexible, resume_quotes, resume_evidence, match_reason, match, jev)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        rows,
    )
    # Exactly what was sent and received, for --show-requests.
    conn.executemany(
        """INSERT INTO jev_requests (assessment_id, request_no, state, questions, answers, model, input_tokens)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        [(cur.lastrowid, n, json.dumps(state), json.dumps({k: q.model_dump() for k, q in questions.items()}),
          json.dumps({k: a.model_dump() for k, a in response.answers.items()}), response.model,
          response.usage.input_tokens)
         for n, ((state, questions), response) in enumerate(zip(requests, responses), 1)],
    )
    conn.commit()


# --- Viewing what Jev was asked -----------------------------------------------------------------

REQUEST_TITLES = {1: "Request 1 · Jev reads the listing", 2: "Request 2 · Jev reads the resume"}


def print_state(state, full):
    """Each state field; long text is cut to its start unless full is set."""
    for key, value in state.items():
        text = json.dumps(value, indent=2) if isinstance(value, dict) else str(value)
        if not full and len(text) > 200:
            text = text[:200].replace("\n", " ") + f"... [{len(text):,} characters; --full shows all]"
        print(f"    {key}: {text}")


def print_question(key, q, answer=None):
    """One question with its allowed answers; with an answer, Jev's probability beside each."""
    print(f"\n  [{key}] {q['type'].upper()}: {q['instructions']}")
    if q["type"] == "noul":
        if answer:
            print(f"      Jev: {answer['noul']:.0%} likely true")
        return
    options = list(q["criteria"].items()) if q["type"] == "choice" else list(enumerate(q["criteria"]))
    probs = {str(k): v for k, v in (answer or {}).get("probabilities", {}).items()}
    top = max(probs, key=probs.get) if probs else None
    for option, text in options:
        if answer:
            p = probs.get(str(option), 0.0)
            mark = "<- top" if str(option) == top else ""
            print(f"      {p:>4.0%}  {option}: {text}  {mark}")
        else:
            print(f"      {option}: {text}")
    if answer and answer.get("confidence") is not None:
        print(f"      confidence {answer['confidence']:.2f}")


def print_requests(job, requests, full):
    """requests: list of (request_no, state, questions_dict, answers_dict or None, model, tokens)."""
    for n, state, questions, answers, model, tokens in requests:
        used = f" · {model} · {tokens:,} tokens" if tokens else ""
        print(f"\n{'=' * 78}\n{REQUEST_TITLES[n]}{used}\n{'=' * 78}")
        print("  STATE (what Jev reads):")
        print_state(state, full)
        print(f"\n  QUESTIONS ({len(questions)}):")
        for key, q in questions.items():
            print_question(key, q, (answers or {}).get(key))


def load_requests(conn, job_id, version):
    """How one job was rated under `version`: where its requirements came from, and every Jev
    request with its answers. None if the job has no Jev result under that version.

    {"requirements": {"source", "model", "instructions", "cost_usd", "items": [...]},
     "requests": [{"request_no", "title", "state", "questions", "answers", "model", "input_tokens"}]}
    """
    a = conn.execute("SELECT id, requirements_version FROM assessments WHERE job_id = ? AND assess_version = ?",
                     (job_id, version)).fetchone()
    if not a:
        return None
    saved = conn.execute("SELECT * FROM jev_requests WHERE assessment_id = ? ORDER BY request_no", (a["id"],)).fetchall()
    if not saved:
        return None  # not a Jev result
    requests = [{"request_no": r["request_no"], "title": REQUEST_TITLES[r["request_no"]], "state": json.loads(r["state"]),
                 "questions": json.loads(r["questions"]), "answers": json.loads(r["answers"]),
                 "model": r["model"], "input_tokens": r["input_tokens"]} for r in saved]

    x = conn.execute("""SELECT x.*, p.system_text FROM extractions x LEFT JOIN prompt_versions p ON p.prompt_hash = x.prompt_hash
                        WHERE x.job_id = ? AND x.requirements_version = ?""",
                     (job_id, a["requirements_version"])).fetchone() if a["requirements_version"] else None
    if x:
        items = [dict(r) for r in conn.execute(
            """SELECT requirement, listing_quote, generic, haiku_importance FROM requirements
               WHERE extraction_id = ? ORDER BY position""", (x["id"],))]
        source = {"source": f"Haiku ({x['requirements_version']})", "model": x["model"],
                  "instructions": x["system_text"], "cost_usd": x["cost_usd"]}
    else:
        # assess-v1-jev rated the lists Sonnet wrote during its own assessment (assess-v1).
        items = [dict(r) for r in conn.execute(
            """SELECT requirement, listing_quote, generic, NULL AS haiku_importance FROM assessment_criteria
               WHERE assessment_id = ? ORDER BY position""", (a["id"],))]
        source = {"source": "Sonnet (assess-v1)", "model": None, "instructions": None, "cost_usd": None}
    for item in items:
        item["generic"] = bool(item["generic"])
    return {"requirements": {**source, "items": items}, "requests": requests}


def show_requests(conn, job_id, version, full):
    job = conn.execute("SELECT title, company FROM jobs WHERE id = ?", (job_id,)).fetchone()
    found = load_requests(conn, job_id, version)
    if not job or not found:
        print(f"Job {job_id} has no {version} result from Jev.")
        return
    print(f"Job {job_id}: {job['title']} · {job['company']} ({version})")
    print(f"Requirements listed by {found['requirements']['source']}")
    print_requests(job, [(r["request_no"], r["state"], r["questions"], r["answers"], r["model"], r["input_tokens"])
                         for r in found["requests"]], full)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--job", type=int, nargs="+", metavar="ID", help="only these job IDs")
    ap.add_argument("--limit", type=int, help="only the first N jobs")
    ap.add_argument("--dry", action="store_true", help="print every question for the first job, no API call")
    ap.add_argument("--show-requests", type=int, metavar="ID", help="print exactly what Jev was asked for a job")
    ap.add_argument("--version", default=VERSION, help=f"with --show-requests: the version (default {VERSION})")
    ap.add_argument("--full", action="store_true", help="with --dry or --show-requests: print the listing and resume in full")
    args = ap.parse_args()

    conn = db.connect()
    resume = RESUME_PATH.read_text()
    prefs = tomllib.loads(Path("preferences.toml").read_text())
    if args.show_requests:
        show_requests(conn, args.show_requests, args.version, args.full)
        return
    jobs = load_jobs(conn, args, include_done=args.dry)
    if not args.dry:
        print(f"{len(jobs)} jobs with {REQUIREMENTS_VERSION} requirements and no {VERSION} result yet")
    if not jobs:
        return

    if args.dry:
        job = jobs[0]
        print(f"\nJob {job['id']}: {job['title']} · {job['company']} · {len(job['criteria'])} requirements")
        print_requests(job, [(n, state, {k: q.model_dump() for k, q in questions.items()}, None, None, None)
                             for n, (state, questions) in enumerate(build_requests(job, resume, prefs), 1)],
                       args.full)
        print(f"\n~{estimate_tokens(job, job['criteria'], resume):,} tokens for this job")
        return

    prompt_hash = fingerprint(resume, prefs)
    older = [row[0] for row in conn.execute(
        """SELECT DISTINCT prompt_hash FROM assessments
           WHERE assess_version = ? AND prompt_hash IS NOT NULL AND prompt_hash != ?""", (VERSION, prompt_hash))]
    if older:
        print(f"WARNING: resume.md, the candidate facts in preferences.toml, or Jev's questions changed since "
              f"{VERSION} results were saved ({', '.join(older)} -> {prompt_hash}). Bump VERSION in assess_jev.py "
              f"to re-rate every job; continuing rates only new jobs, so old and new results would be mixed.")
        if input(f"Continue under {VERSION}? [y/N] ").strip().lower() not in ("y", "yes"):
            return

    load_dotenv()
    if not os.environ.get("TYPESAFE_API_KEY"):
        sys.exit("TYPESAFE_API_KEY isn't set. Add it to .env (and TYPESAFE_BASE_URL if you go through a gateway).")

    tokens = sum(estimate_tokens(j, j["criteria"], resume) for j in jobs)
    print(f"About to rate {len(jobs)} jobs with Jev: ~{tokens:,} input tokens, "
          f"estimated ${tokens * PRICE_PER_M_INPUT / 1_000_000:.4f}")
    if input("Continue? [y/N] ").strip().lower() not in ("y", "yes"):
        print("Cancelled. No calls were made.")
        return

    total_tokens = errors = 0
    with TypeSafeClient() as client:
        for i, job in enumerate(jobs, 1):
            try:
                requests, responses, used = assess(client, job, resume, prefs)
            except (TypeSafeAPIError, TypeSafeError, KeyError) as e:
                # Not saved, so this job is picked up again next run.
                print(f"  [{i}/{len(jobs)}] ERROR {job['title']}: {e}")
                errors += 1
                continue
            save(conn, job, requests, responses, used, prompt_hash)
            total_tokens += used
            print(f"  [{i}/{len(jobs)}] {job['title']} | {job['company']} ({len(job['criteria'])} requirements, "
                  f"{used:,} tokens)")

    print(f"\nDone: {len(jobs) - errors} rated, {errors} errors. {total_tokens:,} tokens, "
          f"${total_tokens * PRICE_PER_M_INPUT / 1_000_000:.4f}")
    print(f"See them in the web UI ({VERSION}), or compare with Sonnet: python compare.py assess-v1 {VERSION}")


if __name__ == "__main__":
    main()
