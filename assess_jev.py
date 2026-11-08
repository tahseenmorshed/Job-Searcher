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
  assess-v3-jev  same as v2, with resume.md updated (TypeScript and the JobSeeker project)
  assess-v4-jev  rates reqs-v2 lists (no duplicates; each item a skill or a duty)

Usage:
  python assess_jev.py                      # every job with a requirement list and no result yet
  python assess_jev.py --limit 5            # the first 5
  python assess_jev.py --job 34             # specific jobs
  python assess_jev.py --job 34 --dry       # print every question for a job, no API call
  python assess_jev.py --show-requests 34   # exactly what Jev was asked for job 34, with its answers
"""

import argparse
import hashlib
import json
import os
import sys
import tomllib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from typesafe_sdk import Choice, Noul, Score, TypeSafeAPIError, TypeSafeClient, TypeSafeError

import db
import spend

# --- Prompts ----------------------------------------------------------------------------------
# Jev gets no instructions block: each question carries its own wording and answer options.
# Changing any of this changes the fingerprint, and the next run warns until VERSION is bumped.

# Request 1 (Jev reads the listing), asked for each requirement. {requirement} is the requirement's
# name, with the listing sentence it came from when there is one (REQUIREMENT_WITH_QUOTE).
REQUIREMENT_WITH_QUOTE = '{requirement} (the listing says: "{quote}")'
IMPORTANCE_QUESTION = "How strongly this listing requires: {requirement}"
FLEXIBLE_QUESTION = ("The listing accepts equivalents for this requirement, for example 'or similar' or "
                     "'or equivalent': {requirement}")
# Request 1, once per job.
CLEARANCE_QUESTION = "What the listing requires about security clearance or citizenship"
# Request 2 (Jev reads the resume), for each requirement. Only the requirement's name: the listing
# sentence can name other skills ("SQL and MongoDB"), which would then count against this one too.
MATCH_QUESTION = ("How well the candidate's resume covers this job requirement: {requirement}. "
                  "Judge from what the resume shows, not from what the candidate could probably learn.")

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

REQUIREMENTS_VERSION = "reqs-v2"  # which requirements.py lists to rate
VERSION = "assess-v4-jev"
PRICE_PER_M_INPUT = 0.042         # USD per million input tokens; output is free
RESUME_PATH = Path("resume.md")


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
    if c.get("listing_quote"):
        return REQUIREMENT_WITH_QUOTE.format(requirement=c["requirement"], quote=c["listing_quote"])
    return c["requirement"]


def listing_questions(criteria):
    questions = {}
    for i, c in enumerate(criteria):
        req = requirement_text(c)
        questions[f"importance_{i}"] = Score(
            instructions=IMPORTANCE_QUESTION.format(requirement=req),
            criteria=list(IMPORTANCE_LEVELS.values()),
        )
        questions[f"flexible_{i}"] = Noul(
            instructions=FLEXIBLE_QUESTION.format(requirement=req),
        )
    questions["clearance"] = Choice(
        instructions=CLEARANCE_QUESTION,
        criteria=CLEARANCE_OPTIONS,
    )
    return questions


def resume_questions(criteria):
    return {
        f"match_{i}": Score(
            instructions=MATCH_QUESTION.format(requirement=c["requirement"]),
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


def load_jobs(conn, limit=None, job_ids=None, include_done=False):
    """Jobs with a REQUIREMENTS_VERSION list (and, unless include_done, no VERSION result yet), each with its criteria."""
    sql = """SELECT j.*, x.id AS extraction_id, x.triage_version FROM jobs j
             JOIN extractions x ON x.job_id = j.id AND x.requirements_version = ?
             WHERE (? OR NOT EXISTS (SELECT 1 FROM assessments a WHERE a.job_id = j.id AND a.assess_version = ?))"""
    params = [REQUIREMENTS_VERSION, include_done, VERSION]
    if job_ids:
        sql += f" AND j.id IN ({','.join('?' * len(job_ids))})"
        params += job_ids
    sql += " ORDER BY j.posted_at DESC"
    jobs = [dict(row) for row in conn.execute(sql, params)][:limit]
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
             json.dumps([REQUIREMENT_WITH_QUOTE, IMPORTANCE_QUESTION, FLEXIBLE_QUESTION, CLEARANCE_QUESTION,
                         MATCH_QUESTION])]
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()[:12]


def build_requests(job, resume, prefs):
    """The two requests for one job: [(state, questions), (state, questions)]."""
    criteria = job["criteria"]
    return [(listing_state(job), listing_questions(criteria)),
            (resume_state(resume, prefs), resume_questions(criteria))]


def token_cost(tokens):
    return tokens * PRICE_PER_M_INPUT / 1_000_000


def assess(client, job, resume, prefs):
    requests = build_requests(job, resume, prefs)
    responses = []
    try:
        for state, questions in requests:
            responses.append(client.system_one(state=state, questions=questions))
    except (TypeSafeAPIError, TypeSafeError) as e:
        if responses:  # the first request was answered and billed before the second failed
            raise spend.BilledError(str(e), token_cost(sum(r.usage.input_tokens or 0 for r in responses))) from e
        raise
    tokens = sum(r.usage.input_tokens or 0 for r in responses)
    return requests, responses, tokens


def save(conn, job, requests, responses, tokens, prompt_hash, run_id=None):
    r1, r2 = responses
    clearance = r1.answers["clearance"]
    cur = conn.execute(
        """INSERT INTO assessments (job_id, assess_version, prompt_hash, model, triage_version,
               clearance_evidence, clearance_requirement, listing_detail, strengths, gaps, lead_with,
               overall_reason, model_rating, cost_usd, assessed_at, requirements_version, run_id)
           VALUES (?, ?, ?, ?, ?, NULL, ?, NULL, '[]', '[]', '[]', ?, NULL, ?, ?, ?, ?)""",
        (job["id"], VERSION, prompt_hash, r1.model, job["triage_version"], clearance.choice,
         f"Rated by {r1.model}; requirements listed by Haiku ({REQUIREMENTS_VERSION}).",
         token_cost(tokens), datetime.now().isoformat(timespec="seconds"), REQUIREMENTS_VERSION, run_id),
    )
    rows = []
    for i, c in enumerate(job["criteria"]):
        imp, flex, match = r1.answers[f"importance_{i}"], r1.answers[f"flexible_{i}"], r2.answers[f"match_{i}"]
        jev = {"importance_probabilities": imp.probabilities, "importance_confidence": imp.confidence,
               "flexible_probability": flex.noul,
               "match_probabilities": match.probabilities, "match_confidence": match.confidence,
               "haiku_importance": c.get("haiku_importance"), "kind": c.get("kind")}
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


@dataclass
class Plan:
    """What a run would do, worked out without spending anything."""
    warning: str | None   # resume, facts or questions changed without VERSION being bumped
    jobs_list: list
    resume: str
    prefs: dict
    prompt_hash: str
    input_tokens: int = 0
    estimate_usd: float = 0.0
    step = "assess_jev"
    label = "Rate with Jev"
    model = "jev"
    version = VERSION

    @property
    def jobs(self):
        return len(self.jobs_list)


def plan(conn, limit=None, job_ids=None, on_status=None):
    """Find jobs with a requirement list and no VERSION result, and estimate the cost (free)."""
    resume = RESUME_PATH.read_text()
    prefs = tomllib.loads(Path("preferences.toml").read_text())
    prompt_hash = fingerprint(resume, prefs)
    older = [row[0] for row in conn.execute(
        """SELECT DISTINCT prompt_hash FROM assessments
           WHERE assess_version = ? AND prompt_hash IS NOT NULL AND prompt_hash != ?""", (VERSION, prompt_hash))]
    warning = None
    if older:
        warning = (f"resume.md, the candidate facts in preferences.toml, or Jev's questions changed since "
                   f"{VERSION} results were saved ({', '.join(older)} -> {prompt_hash}). Bump VERSION in "
                   f"assess_jev.py to re-rate every job; continuing rates only new jobs, so old and new results "
                   f"would be mixed.")
    jobs = load_jobs(conn, limit, job_ids)
    p = Plan(warning=warning, jobs_list=jobs, resume=resume, prefs=prefs, prompt_hash=prompt_hash)
    p.input_tokens = sum(estimate_tokens(j, j["criteria"], resume) for j in jobs)
    p.estimate_usd = token_cost(p.input_tokens)
    return p


def check_key():
    load_dotenv()
    if not os.environ.get("TYPESAFE_API_KEY"):
        raise RuntimeError("TYPESAFE_API_KEY isn't set. Add it to .env (and TYPESAFE_BASE_URL if you go "
                           "through a gateway).")


def run(conn, p: Plan, log=print, should_stop=None):
    """Rate each job's requirements with Jev (paid). Returns how many were rated."""
    if not p.jobs_list:
        return {"rated": 0, "errors": 0, "tokens": 0, "cost_usd": 0.0}
    check_key()
    jobs, total_tokens, errors = p.jobs_list, 0, 0
    with TypeSafeClient() as client, spend.Run(conn, "assess_jev", "typesafe", version=VERSION, model="jev",
                                               planned=len(jobs), estimate_usd=p.estimate_usd) as spent:
        for i, job in enumerate(jobs, 1):
            spend.check_abort(should_stop, i - 1, len(jobs), spent)
            try:
                requests, responses, used = assess(client, job, p.resume, p.prefs)
            except (TypeSafeAPIError, TypeSafeError, KeyError, spend.BilledError) as e:
                # Not saved, so this job is picked up again next run. If one of its two requests
                # was answered before the other failed, that one was billed and is recorded.
                spent.add(getattr(e, "cost_usd", 0.0), failed=1)
                log(f"  [{i}/{len(jobs)}] ERROR {job['title']}: {e}")
                errors += 1
                continue
            spent.add(token_cost(used), ok=1)
            save(conn, job, requests, responses, used, p.prompt_hash, spent.id)
            total_tokens += used
            log(f"  [{i}/{len(jobs)}] {job['title']} | {job['company']} ({len(job['criteria'])} requirements, "
                f"{used:,} tokens)")
    return {"rated": len(jobs) - errors, "errors": errors, "tokens": total_tokens, "cost_usd": spent.cost_usd}


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
    if args.show_requests:
        show_requests(conn, args.show_requests, args.version, args.full)
        return

    if args.dry:
        resume = RESUME_PATH.read_text()
        prefs = tomllib.loads(Path("preferences.toml").read_text())
        jobs = load_jobs(conn, args.limit, args.job, include_done=True)
        if not jobs:
            return
        job = jobs[0]
        print(f"\nJob {job['id']}: {job['title']} · {job['company']} · {len(job['criteria'])} requirements")
        print_requests(job, [(n, state, {k: q.model_dump() for k, q in questions.items()}, None, None, None)
                             for n, (state, questions) in enumerate(build_requests(job, resume, prefs), 1)],
                       args.full)
        print(f"\n~{estimate_tokens(job, job['criteria'], resume):,} tokens for this job")
        return

    p = plan(conn, args.limit, args.job)
    print(f"{p.jobs} jobs with {REQUIREMENTS_VERSION} requirements and no {VERSION} result yet")
    if not p.jobs:
        return
    if p.warning:
        print(f"WARNING: {p.warning}")
        if input(f"Continue under {VERSION}? [y/N] ").strip().lower() not in ("y", "yes"):
            return
    try:
        check_key()
    except RuntimeError as e:
        sys.exit(str(e))

    print(f"About to rate {p.jobs} jobs with Jev: ~{p.input_tokens:,} input tokens, estimated ${p.estimate_usd:.4f}")
    if input("Continue? [y/N] ").strip().lower() not in ("y", "yes"):
        print("Cancelled. No calls were made.")
        return

    r = run(conn, p)
    print(f"\nDone: {r['rated']} rated, {r['errors']} errors. {r['tokens']:,} tokens, ${r['cost_usd']:.4f}")
    print(f"See them in the web UI ({VERSION}), or compare with Sonnet: python compare.py assess-v1 {VERSION}")


if __name__ == "__main__":
    main()
