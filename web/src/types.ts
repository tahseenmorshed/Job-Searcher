// Shapes of the JSON returned by api.py. These mirror the columns selected by results() in report.py.

export type Decision = 'keep' | 'reject'

export type Reason =
  | 'core'
  | 'adjacent'
  | 'open_background'
  | 'too_much_experience'
  | 'not_relevant'
  | 'title_excluded'

export type Source = 'seek' | 'linkedin'

export const SOURCE_LABELS: Record<Source, string> = {
  seek: 'Seek',
  linkedin: 'LinkedIn',
}

export interface Job {
  id: number
  source: Source
  title: string
  company: string | null
  location: string | null
  work_arrangement: string | null
  salary_text: string | null
  posted_at: string | null
  url: string | null
  first_seen_at: string
  decision: Decision
  reason: Reason
  relevance: string | null
  relevance_reason: string | null
  min_years_required: number | null
  years_evidence: string | null
  open_background_evidence: string | null
  title_term: string | null
  assessment: Assessment | null
}

export interface JobsResponse {
  version: string | null
  assess_version: string | null
  jobs: Job[]
}

// From assess.py; score, rating and caps are worked out by scoring.py when the API is called.
export type Rating = 'very_strong' | 'strong' | 'match' | 'stretch' | 'not_suitable'

export const RATING_LABELS: Record<Rating, string> = {
  very_strong: 'Very strong match',
  strong: 'Strong match',
  match: 'Match',
  stretch: 'Stretch',
  not_suitable: 'Not suitable',
}

export interface Criterion {
  requirement: string
  listing_quote: string | null
  generic: boolean
  importance: 'must_have' | 'preferred' | 'bonus'
  flexible: boolean
  resume_quotes: string[]
  resume_evidence: string | null
  match_reason: string | null
  match: 'direct' | 'transferable' | 'side_project' | 'stretch' | 'none'
  weight: number
  score: number
  flags: string[] // quote checks from checks.py (or low-confidence checks for Jev)
  jev: Record<string, unknown> | null // Jev's probabilities; null for Sonnet and Haiku
}

export interface Assessment {
  job_id: number
  assess_version: string
  score: number
  rating: Rating
  caps: string[]
  flag_count: number
  model_rating: Rating | null // null for Jev, which gives no overall rating
  listing_detail: 'detailed' | 'sparse'
  clearance_requirement: string
  clearance_evidence: string | null
  overall_reason: string
  strengths: string[]
  gaps: { gap: string; how_to_address: string }[]
  lead_with: string[]
  criteria: Criterion[]
}

// Same groups and order as REJECTED_GROUPS in report.py.
export const REJECTED_GROUPS: { reason: Reason; label: string }[] = [
  { reason: 'too_much_experience', label: 'Too much experience' },
  { reason: 'not_relevant', label: 'Not relevant' },
  { reason: 'title_excluded', label: 'Title excluded' },
]

// From /api/spend (spend.summary in spend.py).
export interface SpendRun {
  id: number
  step: string
  label: string
  service: string
  version: string | null
  model: string | null
  started_at: string
  finished_at: string | null
  status: 'running' | 'done' | 'aborted' | 'backfilled'
  items_planned: number | null
  items_ok: number
  items_failed: number
  estimate_usd: number | null
  cost_usd: number
}

export interface SpendSummary {
  total_usd: number
  this_month_usd: number
  month: string
  by_step: { step: string; label: string; runs: number; cost_usd: number; items: number; last_run: string }[]
  by_month: { month: string; cost_usd: number; by_step: Record<string, number> }[]
  recent: SpendRun[]
  unlinked: number
  by_source: SourceSpend[]
}

// Everything spent because of one site: its scraping plus the AI steps run on its jobs.
// source is 'unknown' for AI runs that can't be traced to any site's jobs.
export interface SourceSpend {
  source: Source | 'unknown'
  label: string
  cost_usd: number
  by_step: { step: string; label: string; cost_usd: number }[]
  jobs: number
  duplicates: number
  kept: number // kept by the latest triage version
}

// --- Tasks: work started from the page (tasks.py, /api/tasks) ------------------------------

export type FetchSource = 'seek' | 'linkedin'

// What a fetch searches (FetchParams in fetch.py). A source that's null isn't fetched.
export interface FetchParams {
  cities: string[]
  seek: { days: number } | null
  linkedin: { days: number } | null
}

// From GET /api/tasks/options (fetch.options). Each source has its own timeframes.
export interface FetchOptions {
  cities: string[]
  sources: Record<FetchSource, { label: string; days: number[]; default_days: number }>
  defaults: FetchParams
}

export interface TaskOptions {
  fetch: FetchOptions
}

// Apify's cost is only known afterwards, so this is the range of recent fetches; null if none yet.
export interface SourceEstimate {
  estimate_usd: number | null
  low_usd: number | null
  high_usd: number | null
  based_on_runs: number
  longer_than_before: boolean // searching further back than any fetch so far, so it may cost more
  longest_searched_days: number | null
}

// From POST /api/tasks/estimate (pipeline.estimate): the fetch only; the AI steps are estimated
// once the new jobs are in.
export interface FetchEstimate {
  by_source: Partial<Record<FetchSource, SourceEstimate>>
  total_usd: number | null
}

// One source's part of a finished fetch (IngestResult.summary in ingest.py), or why it failed.
export type SourceResult =
  | {
      ok: true
      source: FetchSource
      log_urls: string[]
      items: number
      jobs_found: number
      new: number
      existing: number
      by_city: Record<string, number>
      backup: string
      cost_usd: number | null
    }
  | { ok: false; error: string }

// fetch.run in fetch.py.
export interface FetchResult {
  by_source: Partial<Record<FetchSource, SourceResult>>
  duplicates_hidden: number
  possible_duplicates: number
  new: number
  cost_usd: number
}

// What the Fetch button runs (PipelineParams in pipeline.py): a fetch (or none, to only process
// saved jobs), then the AI steps, each asking before it spends unless auto_approve is set.
export interface PipelineParams {
  fetch: FetchParams | null
  process: boolean
  auto_approve: boolean
}

export type StepStatus = 'pending' | 'running' | 'waiting' | 'done' | 'skipped' | 'stopped' | 'failed'

export interface TaskStep {
  key: 'fetch' | 'triage' | 'requirements' | 'assess'
  label: string
  status: StepStatus
  detail: string | null
}

// A paid step waiting for Continue or Stop (pipeline.run builds it from the step's plan()).
export interface Approval {
  step: TaskStep['key']
  label: string
  jobs: number
  model: string
  version: string
  input_tokens: number
  estimate_usd: number
  warning: string | null // the prompt or resume changed without the version being bumped
  note: string | null
}

export interface PipelineResult {
  fetch?: FetchResult
  steps: TaskStep[]
  aborted?: boolean // stopped part way with the Abort button
}

export interface Task {
  id: number
  kind: 'pipeline'
  params: PipelineParams
  status: 'queued' | 'running' | 'waiting' | 'done' | 'failed'
  progress: string | null
  result: PipelineResult | null
  error: string | null
  created_at: string
  started_at: string | null
  finished_at: string | null
  steps: TaskStep[]
  approval: Approval | null
  abort_requested: boolean // Abort was pressed; it stops after the current job
  log_lines: number
}

// From GET /api/scrapes: the latest fetch of each city from each source.
export interface Scrape {
  source: FetchSource
  city: string
  days: number | null
  scraped_at: string
  jobs_found: number
}
