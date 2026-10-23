import { RATING_LABELS, type Assessment, type Criterion, type Job, type Rating } from './types'

// '2026-10-16T20:00:18.935Z' -> '16 Oct', like posted() in report.py.
function formatDate(value: string | null): string {
  if (!value) return 'date unknown'
  const date = new Date(value)
  if (isNaN(date.getTime())) return 'date unknown'
  return date.toLocaleDateString('en-AU', { day: 'numeric', month: 'short' })
}

// Left border colour per group, so the group is visible at a glance while scrolling.
const ACCENT: Record<string, string> = {
  core: 'border-l-emerald-500',
  adjacent: 'border-l-sky-500',
  open_background: 'border-l-amber-500',
}

export function KeptCard({ job }: { job: Job }) {
  const details = [job.company, job.location, job.work_arrangement, job.salary_text].filter(Boolean)
  const why = job.open_background_evidence || job.relevance_reason

  return (
    <article
      className={`rounded-lg border border-l-4 border-slate-200 bg-white p-4 shadow-sm ${ACCENT[job.reason]} dark:border-slate-700 dark:bg-slate-800`}
    >
      <div className="flex items-baseline justify-between gap-4">
        <h3 className="font-semibold text-slate-900 dark:text-slate-100">{job.title}</h3>
        <span className="flex shrink-0 items-baseline gap-2 text-sm text-slate-500 dark:text-slate-400">
          {job.assessment && <RatingBadge assessment={job.assessment} />}
          {formatDate(job.posted_at)}
        </span>
      </div>

      <p className="mt-1 text-sm text-slate-600 dark:text-slate-300">
        {details.join(' · ')}
        {job.min_years_required !== null && (
          <span className="ml-2 rounded bg-slate-100 px-1.5 py-0.5 text-xs font-medium text-slate-700 dark:bg-slate-700 dark:text-slate-200">
            {job.min_years_required}+ yrs
          </span>
        )}
      </p>

      {why && <p className="mt-3 text-sm text-slate-700 dark:text-slate-300">{why}</p>}

      {job.assessment && <AssessmentDetails assessment={job.assessment} />}

      {job.url && (
        <div className="mt-3 text-right">
          <a
            href={job.url}
            target="_blank"
            rel="noreferrer"
            className="text-sm font-medium text-sky-700 hover:underline dark:text-sky-400"
          >
            View on Seek ↗
          </a>
        </div>
      )}
    </article>
  )
}

// Rejected jobs only need a sanity check, so they get one compact line each.
export function RejectedRow({ job }: { job: Job }) {
  let why = job.relevance_reason
  if (job.reason === 'too_much_experience') why = `${job.min_years_required} yrs · ${job.years_evidence ?? ''}`
  if (job.reason === 'title_excluded') why = `Title contains "${job.title_term}"`

  return (
    <li className="py-2">
      <div className="text-sm">
        {job.url ? (
          <a href={job.url} target="_blank" rel="noreferrer" className="font-medium text-slate-900 hover:underline dark:text-slate-100">
            {job.title}
          </a>
        ) : (
          <span className="font-medium text-slate-900 dark:text-slate-100">{job.title}</span>
        )}
        <span className="text-slate-500 dark:text-slate-400"> · {job.company}</span>
      </div>
      {why && <p className="text-sm text-slate-600 dark:text-slate-400">{why}</p>}
    </li>
  )
}

const RATING_STYLE: Record<Rating, string> = {
  very_strong: 'bg-emerald-600 text-white',
  strong: 'bg-emerald-100 text-emerald-800 dark:bg-emerald-900 dark:text-emerald-100',
  match: 'bg-sky-100 text-sky-800 dark:bg-sky-900 dark:text-sky-100',
  stretch: 'bg-amber-100 text-amber-800 dark:bg-amber-900 dark:text-amber-100',
  not_suitable: 'bg-slate-200 text-slate-700 dark:bg-slate-700 dark:text-slate-200',
}

function RatingBadge({ assessment }: { assessment: Assessment }) {
  return (
    <span className={`rounded px-2 py-0.5 text-xs font-semibold ${RATING_STYLE[assessment.rating]}`}>
      {RATING_LABELS[assessment.rating]} · {assessment.score}
    </span>
  )
}

const IMPORTANCE_LABEL = { must_have: 'Must have', preferred: 'Preferred', bonus: 'Bonus' }
const MATCH_LABEL = {
  direct: 'Direct',
  transferable: 'Transferable',
  side_project: 'Side project',
  stretch: 'Stretch',
  none: 'None',
}
const MATCH_STYLE = {
  direct: 'text-emerald-700 dark:text-emerald-400',
  transferable: 'text-emerald-700 dark:text-emerald-400',
  side_project: 'text-sky-700 dark:text-sky-400',
  stretch: 'text-amber-700 dark:text-amber-400',
  none: 'text-red-700 dark:text-red-400',
}

function AssessmentDetails({ assessment: a }: { assessment: Assessment }) {
  const notes = [...a.caps]
  if (a.model_rating !== a.rating) notes.push(`Model's own rating: ${RATING_LABELS[a.model_rating]}`)
  if (a.listing_detail === 'sparse') notes.push('Sparse listing: rating is less reliable')
  if (a.flag_count > 0)
    notes.push(`${a.flag_count} quote ${a.flag_count === 1 ? 'check' : 'checks'} failed: see the criteria below`)
  if (a.clearance_evidence) notes.push(`Clearance: ${a.clearance_evidence}`)

  return (
    <div className="mt-3 text-sm">
      <p className="text-slate-700 dark:text-slate-300">{a.overall_reason}</p>
      {notes.length > 0 && (
        <ul className="mt-2 space-y-0.5 text-amber-800 dark:text-amber-300">
          {notes.map((n) => (
            <li key={n}>! {n}</li>
          ))}
        </ul>
      )}

      <details className="mt-2">
        <summary className="cursor-pointer text-slate-500 hover:text-slate-800 dark:text-slate-400 dark:hover:text-slate-200">
          Criteria ({a.criteria.length}), gaps and what to lead with
        </summary>
        <ul className="mt-2 divide-y divide-slate-100 dark:divide-slate-700">
          {a.criteria.map((c, i) => (
            <CriterionRow key={i} c={c} />
          ))}
        </ul>
        <List title="Strengths" items={a.strengths} />
        <List title="Gaps" items={a.gaps.map((g) => `${g.gap} — ${g.how_to_address}`)} />
        <List title="Lead with" items={a.lead_with} />
      </details>
    </div>
  )
}

function CriterionRow({ c }: { c: Criterion }) {
  const tags = [IMPORTANCE_LABEL[c.importance], c.flexible && 'accepts equivalents', c.generic && 'generic']
  return (
    <li className="py-2">
      <div className="flex items-baseline justify-between gap-3">
        <span className="font-medium text-slate-800 dark:text-slate-200">{c.requirement}</span>
        <span className={`shrink-0 text-xs font-semibold ${MATCH_STYLE[c.match]}`}>{MATCH_LABEL[c.match]}</span>
      </div>
      <div className="text-xs text-slate-500 dark:text-slate-400">{tags.filter(Boolean).join(' · ')}</div>
      {c.match_reason && <p className="mt-0.5 text-slate-600 dark:text-slate-300">{c.match_reason}</p>}
      {c.resume_evidence && (
        <p className="mt-0.5 text-xs text-slate-500 dark:text-slate-400">Resume: {c.resume_evidence}</p>
      )}
      {c.flags.map((f) => (
        <p key={f} className="mt-0.5 text-xs text-amber-800 dark:text-amber-300">
          ! {f}
        </p>
      ))}
    </li>
  )
}

function List({ title, items }: { title: string; items: string[] }) {
  if (items.length === 0) return null
  return (
    <div className="mt-3">
      <h4 className="text-xs font-semibold tracking-wider text-slate-500 uppercase dark:text-slate-400">{title}</h4>
      <ul className="mt-1 list-disc space-y-0.5 pl-5 text-slate-700 dark:text-slate-300">
        {items.map((item) => (
          <li key={item}>{item}</li>
        ))}
      </ul>
    </div>
  )
}
