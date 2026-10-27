# JobSeeker

JobSeeker fetches software job listings from Seek, filters out the ones that don't fit, and rates the rest against your resume, one requirement at a time. A web page shows the results, best match first, with a full breakdown of how each rating was reached.

Most of the work is done by AI, but every rating can be traced back to the exact questions asked and the answers given. The final score is worked out by plain code from those answers, so it's consistent and you can tune it without paying for anything to be re-run.

## How it works

```
 Seek ──► ingest.py ──► evaluate.py ──► requirements.py ──► assess_jev.py ──► scoring.py ──► web UI
          (Apify)       (Haiku)         (Haiku)             (Jev)              (code)
          fetch         triage          list the            rate each          score and
          listings      core/adjacent/  requirements        requirement        rating
                        rejected
```

1. **Fetch** (`ingest.py`): an Apify scraper pulls recent Seek listings and saves them to a local SQLite database. Each run also writes a JSON backup to `data/`.
2. **Triage** (`evaluate.py`, Claude Haiku): each new listing is sorted into **core** (software engineering), **adjacent** (DevOps, data, QA and similar) or **rejected**. Titles like "Principal" are rejected without an AI call, and jobs asking for far more experience than you have are rejected too. Only core jobs go further.
3. **List the requirements** (`requirements.py`, Claude Haiku): Haiku reads the listing, never your resume, and writes out each requirement as a stand-alone name plus the listing sentence it came from. "Java and Kubernetes" becomes two requirements, while "Java, C# or Go" stays one, since any of them is enough.
4. **Rate each requirement** (`assess_jev.py`, Jev): [Jev](https://docs.typesafe.ai) is a model from TypeSafe that never writes text. It answers fixed questions with a probability for each possible answer. For every requirement it answers two questions:
   - **How essential is it?** It reads the listing and picks must-have, preferred or bonus.
   - **How well do you match it?** It reads your resume and picks direct, transferable, side project, stretch or none.
5. **Score** (`scoring.py`, plain code): each requirement is worth points by how essential it is, and you earn a share of them by how well you match. The total becomes a score out of 100 and a rating from "Very strong match" to "Not suitable". See [How the score is worked out](#how-the-score-is-worked-out).
6. **Review** (web UI): core jobs are listed best first. Each card opens into a step-by-step breakdown: the listing sentence, the exact question Jev was asked, its answer as bars, and the points earned.

### Nothing is paid for twice

Every AI result is saved under a **version name** (`triage-v3`, `reqs-v1`, `assess-v2-jev`). Each script only processes jobs that don't yet have a result under its current version, and shows the count and estimated cost before spending anything. Failed jobs aren't saved, so they're retried on the next run.

To redo a step on purpose, for example after changing its instructions, change its version name. Old results stay in the database under the old name, so you can compare before and after.

## Setup

You need Python 3.11+, Node.js and three API keys.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cd web && npm install && cd ..
```

Create a `.env` file in the project folder:

```
APIFY_TOKEN=...          # apify.com: runs the Seek scraper
ANTHROPIC_API_KEY=...    # console.anthropic.com: Claude Haiku (and Sonnet, optionally)
TYPESAFE_API_KEY=...     # typesafe.ai: Jev
# TYPESAFE_BASE_URL=...  # only if you reach Jev through a gateway such as OpenRouter
```

Then add your own details (see [Customising](#customising)):

- `resume.md`: your full resume as plain text or Markdown
- `profile.md`: a short summary and the guide triage uses to decide what counts as core
- `preferences.toml`: your years of experience, titles to reject, work rights and clearance

## Running it

Run these from the project folder with the virtual environment active. Every step that spends money asks first.

```bash
python ingest.py           # fetch new listings from Seek
python evaluate.py         # triage the new ones
python requirements.py     # Haiku lists requirements for new core jobs
python assess_jev.py       # Jev rates them
```

Then start the web UI, in two terminals:

```bash
uvicorn api:app --reload   # the API, on port 8000
```

```bash
cd web && npm run dev      # the page, usually at http://localhost:5173
```

Start the API first; the page shows "Could not reach the API" without it. Restart the API after changing any Python file it uses.

### Trying a few jobs first

Most scripts take `--limit N` and `--job ID ...`:

```bash
python requirements.py --limit 5
python assess_jev.py --limit 5
python assess_jev.py --job 34 --dry   # print every question Jev would be asked, without calling it
```

## The web UI

- **Tabs:** Core, Adjacent (plus "open to software background") and Rejected, grouped by reason.
- **Filters:** posting date and city.
- **Version dropdowns:** the triage version, and the assessment version (for example `assess-v2-jev` for Jev, `assess-v1` for Sonnet).
- **Core cards:** a rating badge and score, warnings (such as a missing must-have, a clearance requirement, or answers Jev was unsure about), and two expandable sections:
  - **Criteria:** every requirement with its importance, your match and any checks to review.
  - **How this job was rated, step by step** (Jev versions): for each requirement, the listing sentence Haiku found, Jev's importance question and answer, and Jev's match question and answer, plus exactly what Haiku was told and what Jev read.

## How the score is worked out

Each requirement is **worth** points, according to how essential Jev judged it:

| How essential | Worth |
|---|---|
| Must-have | 3 |
| Preferred | 2 |
| Bonus | 1 |

Generic requirements that nearly anyone could claim ("good communication") are worth a quarter of that.

You **earn** a share of those points, according to how well Jev judged you match:

| Your match | Earned |
|---|---|
| Direct: done professionally | 100% |
| Transferable: a close equivalent, such as C# for Java | 75% |
| Side project: done in study or a side project | 50% |
| Stretch: related, with a real gap | 25% |
| None | 0% |

If the listing accepts equivalents ("or similar"), transferable counts as 100%.

**Score = points earned ÷ points possible × 100.** This keeps listings with 5 requirements and listings with 20 on the same scale.

The score sets the rating:

| Score | Rating |
|---|---|
| 85+ | Very strong match |
| 70–84 | Strong match |
| 55–69 | Match |
| 35–54 | Stretch |
| Below 35 | Not suitable |

Then **caps** stop a good average from hiding a deal-breaker:

| If | The rating can't go above |
|---|---|
| A non-generic must-have you're rated "none" in | Stretch |
| A non-generic must-have you're rated "stretch" in | Match |
| The job needs a security clearance you must already hold, and you hold none | Not suitable |

The score and rating are worked out each time results are shown, never stored. Changing a number in `scoring.py` re-rates every saved job instantly, with no new AI calls.

## Customising

| To change | Edit | Then |
|---|---|---|
| What's searched on Seek (keyword, cities, days back, pages) | the constants at the top of `ingest.py` | nothing |
| What counts as core, adjacent or not relevant | the relevance guide in `profile.md` | bump `PROMPT_VERSION` in `evaluate.py` to re-triage |
| Your years of experience, the gap you'll accept, titles to reject | `preferences.toml` | bump `PROMPT_VERSION` in `evaluate.py` |
| Your work rights and current clearance | `[candidate]` in `preferences.toml` | the clearance cap applies immediately; bump `VERSION` in `assess_jev.py` for Jev to see the new facts |
| Your experience | `resume.md` | bump `VERSION` in `assess_jev.py` to re-rate every job (it warns you if you forget) |
| How Haiku lists requirements | `SYSTEM` in `requirements.py` | bump `REQUIREMENTS_VERSION`, and `VERSION` in `assess_jev.py` |
| Jev's questions and answer levels | `IMPORTANCE_LEVELS`, `MATCH_LEVELS`, `CLEARANCE_OPTIONS` and the question builders in `assess_jev.py` | bump `VERSION` in `assess_jev.py` |
| Points, match shares, generic weight, rating cut-offs | the constants at the top of `scoring.py` | nothing: every job is re-rated instantly |
| Which caps apply | `rate()` in `scoring.py` | nothing |
| When a Jev answer is flagged as unsure | `LOW_CONFIDENCE` in `checks.py` | nothing |

Each AI step warns you before running if its instructions, questions, resume or profile changed since results were saved under the current version, so you don't forget to bump it.

## Other tools

### Comparing two versions

```bash
python compare.py assess-v1 assess-v2-jev           # summary
python compare.py assess-v1 assess-v2-jev --job 34  # one job, requirement by requirement
```

This shows how often two versions agree on ratings, scores, importance and match, where they differ, and which jobs moved most. It's how Sonnet, Haiku and Jev were compared while building this. It reads saved results only, with no AI calls.

### Seeing exactly what Jev was asked

```bash
python assess_jev.py --show-requests 34          # every question and answer for job 34
python assess_jev.py --show-requests 34 --full   # including the full listing and resume Jev read
```

Every Jev request is saved exactly as sent, so this stays accurate even after the question code changes. The same information appears in the web UI's step-by-step breakdown.

### The Claude assessment (alternative to Jev)

`assess.py` rates core jobs with Claude alone, in one call per job: it lists the requirements, rates each one, quotes evidence from the listing and your resume, and writes strengths, gaps and what to lead with. It gives written explanations, which Jev can't, at a much higher cost.

```bash
python assess.py                     # Sonnet, saved as assess-v1
python assess.py --model haiku       # Haiku, saved as assess-v1-haiku
python assess.py --show              # results, best first
python assess.py --check             # quotes that couldn't be found in the listing or resume
```

`checks.py` verifies its quotes loosely: a quote passes if most of its words appear close together in the source.

### Text reports

```bash
python report.py              # triage results for the latest version
python report.py --new        # only jobs first seen in the last 24 hours
python report.py --only core  # one group
python report.py --compare triage-v2   # jobs whose triage result changed
```

## Costs

Measured on real runs unless marked as an estimate:

| Step | Model | Per job |
|---|---|---|
| Triage | Haiku | $0.00024 |
| List requirements | Haiku | about $0.0005–0.001 (estimate) |
| Rate requirements | Jev | about $0.0003 (estimate) |
| Full assessment, the alternative | Sonnet | $0.035 |
| Full assessment, the alternative | Haiku | $0.0033 |

Seek scraping through Apify is charged separately by Apify, and `ingest.py` prints the cost of each run.

## Files

| File | What it does |
|---|---|
| `ingest.py` | Fetches Seek listings through Apify and saves them |
| `evaluate.py` | Triage with Haiku: core, adjacent or rejected |
| `requirements.py` | Haiku lists each core job's requirements |
| `assess_jev.py` | Jev rates each requirement; also `--dry` and `--show-requests` |
| `assess.py` | The alternative: a full assessment with Sonnet or Haiku |
| `scoring.py` | Turns ratings into a score, a rating and caps |
| `checks.py` | Quote checks for Claude assessments; confidence checks for Jev |
| `compare.py` | Compares two assessment versions |
| `report.py` | Text report of triage results |
| `api.py` | The HTTP API the web UI reads from |
| `db.py` | The database schema and saving listings |
| `import_existing.py` | One-off import of older JSON files and triage results |
| `web/` | The React web UI |
| `resume.md`, `profile.md`, `preferences.toml` | Your details |
| `data/jobseeker.db` | The database: listings, every AI result, every prompt and every Jev request |

`config.toml` isn't read by any script. It was an earlier plan for search settings across Seek, Indeed and LinkedIn; `ingest.py` uses its own constants instead.

### Database tables

| Table | Holds |
|---|---|
| `jobs` | Every listing fetched |
| `evaluations` | Triage results, one per job per triage version |
| `extractions`, `requirements` | Haiku's requirement lists |
| `assessments`, `assessment_criteria` | Ratings per job and per requirement, from any model |
| `jev_requests` | Every Jev request exactly as sent, with its answers |
| `prompt_versions` | The exact instructions behind every version |

`data/` is in `.gitignore`, so your listings and results stay on your machine.
