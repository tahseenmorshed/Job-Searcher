# JobSeeker

JobSeeker fetches software job listings from Seek and LinkedIn, filters out the ones that don't fit, and rates the rest against your resume, one requirement at a time. A web page runs the whole thing with one button and shows the results, best match first, with a full breakdown of how each rating was reached.

Most of the work is done by AI, but every rating can be traced back to the exact questions asked and the answers given. The final score is worked out by plain code from those answers, so it's consistent and you can tune it without paying for anything to be re-run.

## How it works

```
 Seek ──────► ingest.py ─────────┐
 LinkedIn ──► ingest_linkedin.py ┴► triage.py ──► requirements.py ──► assess_jev.py ──► scoring.py ──► web UI
              (Apify)               (Haiku)       (Haiku)             (Jev)              (code)
              fetch listings        triage        list the            rate each          score and
                                    core/         requirements        requirement        rating
                                    adjacent/
                                    rejected
```

The **Fetch listings** button on the page runs all of these in order (`pipeline.py`). You can also run each step as a script.

1. **Fetch** (`ingest.py`, `ingest_linkedin.py`): Apify scrapers pull recent Seek and LinkedIn listings for Perth, Melbourne and Sydney and save them to a local SQLite database. Each run also writes a JSON backup to `data/`. Listings that are the same job as one already saved, from either site, are hidden (`dedupe.py`).
2. **Triage** (`triage.py`, Claude Haiku): each new listing is sorted into **core** (software engineering), **adjacent** (DevOps, data, QA and similar) or **rejected**. Titles like "Principal" are rejected without an AI call, and jobs asking for far more experience than you have are rejected too. Only core jobs go further.
3. **List the requirements** (`requirements.py`, Claude Haiku): Haiku reads the listing, never your resume, and writes out each requirement as a stand-alone name plus the listing sentence it came from. "Java and Kubernetes" becomes two requirements, while "Java, C# or Go" stays one, since any of them is enough.
4. **Rate each requirement** (`assess_jev.py`, Jev): [Jev](https://docs.typesafe.ai) is a model from TypeSafe that never writes text. It answers fixed questions with a probability for each possible answer. For every requirement it answers two questions:
   - **How essential is it?** It reads the listing and picks must-have, preferred or bonus.
   - **How well do you match it?** It reads your resume and picks direct, transferable, side project, stretch or none.
5. **Score** (`scoring.py`, plain code): each requirement is worth points by how essential it is, and you earn a share of them by how well you match. The total becomes a score out of 100 and a rating from "Very strong match" to "Not suitable". See [How the score is worked out](#how-the-score-is-worked-out).
6. **Review** (web UI): core jobs are listed best first. Each card opens into a step-by-step breakdown: the listing sentence, the exact question Jev was asked, its answer as bars, and the points earned.

### Nothing is paid for twice

Every AI result is saved under a **version name** (`triage-v4`, `reqs-v2`, `assess-v4-jev`). Each script only processes jobs that don't yet have a result under its current version, and shows the count and estimated cost before spending anything. Failed jobs aren't saved, so they're retried on the next run. The same goes for jobs left over when you stop or abort a run.

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
APIFY_TOKEN=...          # apify.com: runs the Seek and LinkedIn scrapers
ANTHROPIC_API_KEY=...    # console.anthropic.com: Claude Haiku (and Sonnet, optionally)
TYPESAFE_API_KEY=...     # typesafe.ai: Jev
# TYPESAFE_BASE_URL=...  # only if you reach Jev through a gateway such as OpenRouter
```

Then add your own details (see [Customising](#customising)):

- `resume.md`: your full resume as plain text or Markdown
- `profile.md`: a short summary and the guide triage uses to decide what counts as core
- `preferences.toml`: your years of experience, titles to reject, work rights and clearance

## Running it

Run these from the project folder with the virtual environment active.

### Start the app

In two terminals:

```bash
uvicorn api:app --reload   # the API, on port 8000
```

```bash
cd web && npm run dev      # the page, at http://localhost:5173
```

Start the API first; the page shows "Could not reach the API" without it. `--reload` restarts the API when you save a Python file, so you don't need to restart it by hand. Don't edit Python files during a run, though: the restart cuts the run off (see below).

### Fetching and processing from the page

Click **Fetch listings** in the page header to open the panel.

1. **Pick cities:** any of Perth, Melbourne and Sydney.
2. **Pick sites:** Seek, LinkedIn or both, each with its own timeframe. Seek offers 1, 3, 7, 14 or 31 days; LinkedIn's scraper offers 1, 7 or 30. The panel shows the fetch's estimated cost, based on recent runs.
3. **Then:** leave **Triage, list requirements and rate new jobs** ticked to process the new jobs straight after the fetch. Tick **Don't ask before each step** to run every step without stopping for approval.
4. Click **Fetch now**. Or click **Process saved jobs** to skip the fetch and only process jobs already saved but not yet done.

While it runs:

- **Step list:** each step shows its status and progress, such as "12 of 40".
- **Approval:** before each AI step, the panel shows how many jobs it will process, the model and the estimated cost, and waits for **Continue** or **Stop here**, like the scripts' y/N. Stopping skips the later steps too, since each works on what the one before produced. The panel stays open while a question is waiting, and a question left unanswered for an hour counts as Stop.
- **Abort:** stops a run part way. The job in progress finishes first, so the button shows "Stopping…" for a moment. Jobs already done, and what was spent on them, are kept, and the rest are picked up next run. During a fetch, the scrape can't be cut short (Apify is already running it), so the fetch finishes and saves its jobs, then the run stops before any AI step. Pressed while a question is waiting, Abort counts as Stop.
- **Log:** a small window shows each job as it's fetched, triaged and rated, like the scripts print. The API keeps it in memory only, so it's gone after a restart.
- **Last fetched:** a table shows when each city was last fetched from each site.

When the run finishes or is aborted, the job list and spend totals reload by themselves. Only one run can go at a time. If you reload the page mid-run, it picks the run up again.

Both sites are fetched at the same time, and duplicates are checked once at the end. If one site fails, the other's jobs are still saved and processed.

Triage reads `profile.md` and `preferences.toml` when the API starts. With `--reload` that's handled for Python files only, so restart the API after changing either. Rating reads `resume.md` fresh each run, and the scripts always read the current files.

If the API restarts mid-run (an edit with `--reload`, or a crash), the run is marked failed. Anything already saved stays saved, but Apify runs that had started may still finish and be billed.

### Running each step as a script

The page and the scripts use the same code, so you can do it all from the terminal instead. Every step that spends money shows its count and estimated cost and asks first. Ctrl-C stops a step part way and keeps what was done.

```bash
python ingest.py           # fetch from Seek (--city Perth, --days 3, --max-pages, --dry-run)
python ingest_linkedin.py  # fetch from LinkedIn (--city, --days 1/7/30, --dry-run, --from-file)
python triage.py           # triage the new ones
python requirements.py     # Haiku lists requirements for new core jobs
python assess_jev.py       # Jev rates them
```

Results show up in the page the next time it loads.

### Over HTTP

The page uses these endpoints, so you can drive a run with `curl` too:

```bash
# what the fetch would cost (free); the AI steps are estimated when they're reached
curl -X POST localhost:8000/api/tasks/estimate -H 'Content-Type: application/json' \
     -d '{"kind": "pipeline", "params": {"fetch": {"cities": ["Perth"], "seek": {"days": 3}, "linkedin": null}}}'
# start it: answers 202 with the task, or 409 if one is already running
curl -X POST localhost:8000/api/tasks -H 'Content-Type: application/json' \
     -d '{"kind": "pipeline", "params": {"fetch": {"cities": ["Perth"], "seek": {"days": 3}, "linkedin": null}}}'
curl localhost:8000/api/tasks/1                 # status, steps, and the question it's waiting on
curl -X POST localhost:8000/api/tasks/1/approval -H 'Content-Type: application/json' -d '{"approve": true}'
curl -X POST localhost:8000/api/tasks/1/abort   # stop after the job in progress
curl localhost:8000/api/tasks/1/log?after=0     # the log, from line 0
curl localhost:8000/api/scrapes                 # when each city was last fetched from each site
```

Set a site to `null` to skip it, or set `"fetch": null` to only process saved jobs. Add `"auto_approve": true` to skip the approval questions. Timeframes a site doesn't offer are refused before Apify is called.

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
| The Seek search keyword and city list | `SEARCH_SLUG` and `SEEK_LOCATIONS` at the top of `ingest.py` | nothing (cities and days are picked on the page, or with `--city` and `--days`) |
| The LinkedIn search keyword and city list | `KEYWORDS` and `LINKEDIN_LOCATIONS` at the top of `ingest_linkedin.py` | nothing |
| When two listings count as the same job | `DUPLICATE_SIMILARITY`, `POSSIBLE_SIMILARITY` and `MAX_DAYS_APART` at the top of `dedupe.py` | `python dedupe.py` to re-check every listing |
| How Haiku triages listings | `SYSTEM_PROMPT` and `LISTING_PROMPT` at the top of `triage.py` | bump `PROMPT_VERSION` in `triage.py` |
| What counts as core, adjacent or not relevant | the relevance guide in `profile.md` | bump `PROMPT_VERSION` in `triage.py` to re-triage, and restart the API |
| Your years of experience, the gap you'll accept, titles to reject | `preferences.toml` | bump `PROMPT_VERSION` in `triage.py`, and restart the API |
| Your work rights and current clearance | `[candidate]` in `preferences.toml` | the clearance cap applies immediately; bump `VERSION` in `assess_jev.py` for Jev to see the new facts |
| Your experience | `resume.md` | bump `VERSION` in `assess_jev.py` to re-rate every job (it warns you if you forget) |
| How Haiku lists requirements | `SYSTEM_PROMPT` and `LISTING_PROMPT` at the top of `requirements.py` | bump `REQUIREMENTS_VERSION`, and `VERSION` in `assess_jev.py` |
| Jev's questions and answer levels | the question templates (`IMPORTANCE_QUESTION` and others) and answer levels at the top of `assess_jev.py` | bump `VERSION` in `assess_jev.py` |
| Points, match shares, generic weight, rating cut-offs | the constants at the top of `scoring.py` | nothing: every job is re-rated instantly |
| Which caps apply | `rate()` in `scoring.py` | nothing |
| When a Jev answer is flagged as unsure | `LOW_CONFIDENCE` in `checks.py` | nothing |

Each AI step warns you before running if its instructions, questions, resume or profile changed since results were saved under the current version, so you don't forget to bump it. On the page, the warning appears in the approval question and the log.

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

Seek and LinkedIn scraping is charged by Apify. Its cost is saved after each fetch, and the page's fetch estimate is based on it.

### Seeing what you've spent

Every paid run is saved to the `runs` table: when it ran, which step and version, how many jobs, the estimate you agreed to and the actual cost. The row is written as soon as you agree and updated after every job, so a crash or Ctrl-C still keeps what was spent. Calls that were billed but then failed (a cut-off answer, say) are counted too. Each saved result links to the run that paid for it (`run_id`).

```bash
python spend.py              # total, this month, by step, by month, and the latest runs
python spend.py --runs 30    # more runs
python spend.py --backfill   # add spend saved before runs were recorded, and fetch Apify run costs
```

`--backfill` is safe to run again: it only adds what's missing and refreshes Apify costs, which Apify can finalise a little after a run ends. It fetches every run on your Apify account, not only the Seek scraper's, and shows each actor's name.

In the web UI, the **Spent $…** button at the top opens the same totals and latest runs.

Anthropic and Jev costs are worked out from token counts at the prices set in each script, so they're close to, not exactly, what you're billed. Triage results from `triage-v1` were imported without costs, so they aren't counted.

## Files

| File | What it does |
|---|---|
| `fetch.py` | Fetches from the sites and cities you pick, both sites at once, then checks for duplicates; used by the page |
| `ingest.py` | Fetches Seek listings through Apify and saves them |
| `ingest_linkedin.py` | Fetches LinkedIn listings through Apify and saves them (`--from-file` loads a saved backup for free) |
| `dedupe.py` | Hides listings that are the same job as another, from either site; runs after each fetch (`--show` lists the pairs) |
| `triage.py` | Triage with Haiku: core, adjacent or rejected |
| `requirements.py` | Haiku lists each core job's requirements |
| `assess_jev.py` | Jev rates each requirement; also `--dry` and `--show-requests` |
| `assess.py` | The alternative: a full assessment with Sonnet or Haiku |
| `scoring.py` | Turns ratings into a score, a rating and caps |
| `checks.py` | Quote checks for Claude assessments; confidence checks for Jev |
| `compare.py` | Compares two assessment versions |
| `report.py` | Text report of triage results |
| `pipeline.py` | Fetch, then triage, list requirements and rate, asking before each paid step: the page's Fetch button |
| `tasks.py` | Tracks runs started from the page: status, steps, log, approval questions and abort |
| `scrapes.py` | Records when each city was last fetched from each site (`python scrapes.py`; `--backfill` reads old backups in `data/`) |
| `api.py` | The HTTP API the web UI uses: jobs, spend, and starting and following runs |
| `spend.py` | Records what each run spends, and shows it (`python spend.py`) |
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
| `runs` | Every paid run: step, version, jobs, estimate and actual cost |
| `job_matches` | Pairs of listings found to be the same job |
| `tasks` | Runs started from the page, with their settings and result |
| `scrapes` | When each city was last fetched from each site |

`data/` is in `.gitignore`, so your listings and results stay on your machine.
