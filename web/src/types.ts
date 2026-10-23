// Shapes of the JSON returned by api.py. These mirror the columns selected by results() in report.py.

export type Decision = 'keep' | 'reject'

export type Reason =
  | 'core'
  | 'adjacent'
  | 'open_background'
  | 'too_much_experience'
  | 'not_relevant'
  | 'title_excluded'

export interface Job {
  id: number
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
  flags: string[] // quote checks from checks.py
}

export interface Assessment {
  score: number
  rating: Rating
  caps: string[]
  flag_count: number
  model_rating: Rating
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
