import { useState, type ReactNode } from 'react'
import type { Criterion } from './types'

// Shapes returned by GET /api/jev-requests/{job_id} (load_requests in assess_jev.py).
interface JevQuestion {
  type: 'score' | 'choice' | 'noul'
  instructions: string
  criteria?: string[] | Record<string, string | null>
}
interface JevAnswer {
  type?: string
  probabilities?: Record<string, number>
  confidence?: number | null
  noul?: number
}
interface JevRequest {
  request_no: number
  title: string
  state: Record<string, unknown>
  questions: Record<string, JevQuestion>
  answers: Record<string, JevAnswer>
  model: string | null
  input_tokens: number | null
}
interface ListedRequirement {
  requirement: string
  listing_quote: string | null
  generic: boolean
  haiku_importance: string | null // Haiku's own guess; not used for scoring
}
interface JevRequestsResponse {
  requirements: {
    source: string // e.g. "Haiku (reqs-v1)" or "Sonnet (assess-v1)"
    model: string | null
    instructions: string | null // exactly what Haiku was told
    cost_usd: number | null
    items: ListedRequirement[]
  }
  requests: JevRequest[]
}

const IMPORTANCE_LABELS: Record<string, string> = { must_have: 'Must-have', preferred: 'Preferred', bonus: 'Bonus' }
// Jev's importance levels are numbered lowest first: 0 bonus, 1 preferred, 2 must-have.
const IMPORTANCE_BY_LEVEL = ['bonus', 'preferred', 'must_have']

const LOW_CONFIDENCE = 0.5 // same threshold as checks.py

// "Required: stated as required..." -> ["Required", "stated as required..."]
function splitLevel(text: string): [string, string] {
  const i = text.indexOf(':')
  return i > 0 ? [text.slice(0, i), text.slice(i + 1).trim()] : [text, '']
}

// The options of a question as [key, short label, full description], lowest level first.
function optionsOf(q: JevQuestion): [string, string, string][] {
  if (Array.isArray(q.criteria)) return q.criteria.map((text, i) => [String(i), ...splitLevel(text)])
  return Object.entries(q.criteria ?? {}).map(([key, text]) => [key, key.replace(/_/g, ' '), text ?? ''])
}

// Show the listing sentence inside a question as a highlighted span, so it's obvious what extra
// text Jev saw alongside the requirement's name.
function QuestionText({ text }: { text: string }) {
  const m = text.match(/^(.*?)\s*\(the listing says: "(.*)"\)(.*)$/s)
  if (!m) return <p className="text-slate-700 dark:text-slate-300">{text}</p>
  return (
    <p className="text-slate-700 dark:text-slate-300">
      {m[1]}{' '}
      <span
        className="rounded bg-orange-100 px-1 text-orange-900 dark:bg-orange-950 dark:text-orange-200"
        title="The listing sentence this requirement came from, included in the question"
      >
        (the listing says: "{m[2]}")
      </span>
      {m[3]}
    </p>
  )
}

function Confidence({ value }: { value: number | null | undefined }) {
  if (value === null || value === undefined) return null
  const low = value < LOW_CONFIDENCE
  return (
    <span
      className={`rounded px-1.5 py-0.5 text-xs font-medium ${
        low
          ? 'bg-amber-100 text-amber-800 dark:bg-amber-900 dark:text-amber-100'
          : 'bg-slate-100 text-slate-600 dark:bg-slate-700 dark:text-slate-300'
      }`}
      title="How concentrated Jev's answer is (0 to 1). It is not the chance the answer is right."
    >
      {low ? 'unsure · ' : ''}confidence {value.toFixed(2)}
    </span>
  )
}

// Jev's probability for every allowed answer, highest first visually marked.
function Bars({ q, a }: { q: JevQuestion; a: JevAnswer | undefined }) {
  const probs = a?.probabilities ?? {}
  const options = optionsOf(q).slice().reverse() // best level first reads more naturally
  const top = Object.keys(probs).reduce<string | null>((best, k) => (best === null || probs[k] > probs[best] ? k : best), null)
  return (
    <div className="mt-1.5 space-y-1">
      {options.map(([key, label, description]) => {
        const p = probs[key] ?? 0
        const isTop = key === top
        return (
          <div key={key} className="grid grid-cols-[7.5rem_1fr_2.75rem] items-center gap-2" title={description}>
            <span className={`truncate ${isTop ? 'font-semibold text-teal-700 dark:text-teal-300' : 'text-slate-600 dark:text-slate-400'}`}>
              {label}
            </span>
            <span className="h-2.5 overflow-hidden rounded bg-slate-200 dark:bg-slate-700">
              <span
                className={`block h-full rounded ${isTop ? 'bg-teal-600 dark:bg-teal-400' : 'bg-teal-600/40 dark:bg-teal-400/40'}`}
                style={{ width: `${Math.round(p * 100)}%` }}
              />
            </span>
            <span className={`text-right tabular-nums ${isTop ? 'font-semibold text-teal-700 dark:text-teal-300' : 'text-slate-500'}`}>
              {Math.round(p * 100)}%
            </span>
          </div>
        )
      })}
    </div>
  )
}

function QuestionBlock({ step, heading, q, a }: { step?: number; heading: string; q: JevQuestion | undefined; a: JevAnswer | undefined }) {
  if (!q) return null
  return (
    <div className="min-w-0 rounded-md border border-slate-200 p-3 dark:border-slate-700">
      <div className="flex flex-wrap items-center justify-between gap-2">
        {step ? (
          <StepLabel n={step} who="Jev">{heading}</StepLabel>
        ) : (
          <h5 className="text-xs font-semibold tracking-wide text-slate-500 uppercase dark:text-slate-400">{heading}</h5>
        )}
        <Confidence value={a?.confidence} />
      </div>
      <div className="mt-1 text-xs">
        <span className="font-medium text-slate-500 dark:text-slate-400">Question sent to Jev: </span>
        <QuestionText text={q.instructions} />
      </div>
      {q.type === 'noul' ? (
        <p className="mt-1.5">
          Jev: <span className="font-semibold tabular-nums">{Math.round((a?.noul ?? 0) * 100)}%</span> likely true
        </p>
      ) : (
        <Bars q={q} a={a} />
      )}
    </div>
  )
}

function StepLabel({ n, who, children }: { n: number; who: 'Haiku' | 'Jev'; children: ReactNode }) {
  const colour =
    who === 'Haiku'
      ? 'bg-amber-100 text-amber-800 dark:bg-amber-900 dark:text-amber-100'
      : 'bg-teal-100 text-teal-800 dark:bg-teal-900 dark:text-teal-100'
  return (
    <div className="flex flex-wrap items-center gap-2">
      <span className="flex h-5 w-5 items-center justify-center rounded-full bg-slate-700 text-[11px] font-bold text-white dark:bg-slate-300 dark:text-slate-900">
        {n}
      </span>
      <h5 className="text-xs font-semibold tracking-wide text-slate-600 uppercase dark:text-slate-300">{children}</h5>
      <span className={`rounded px-1.5 py-0.5 text-[11px] font-semibold ${colour}`}>{who}</span>
    </div>
  )
}

// The most likely importance level in Jev's answer, as our label.
function jevImportance(a: JevAnswer | undefined): string | null {
  const probs = a?.probabilities
  if (!probs || Object.keys(probs).length === 0) return null
  const top = Object.keys(probs).reduce((best, k) => (probs[k] > probs[best] ? k : best))
  return IMPORTANCE_BY_LEVEL[Number(top)] ?? null
}

function RequirementCard({
  index,
  c,
  listed,
  listing,
  resume,
}: {
  index: number
  c: Criterion | undefined
  listed: ListedRequirement | undefined
  listing?: JevRequest
  resume?: JevRequest
}) {
  const name = c?.requirement ?? listed?.requirement ?? `Requirement ${index + 1}`
  const importance = listing?.answers[`importance_${index}`]
  const flex = listing?.answers[`flexible_${index}`]
  const jevChoice = jevImportance(importance)
  const haiku = listed?.haiku_importance
  return (
    <li className="rounded-lg border border-slate-200 bg-slate-50 p-3 dark:border-slate-700 dark:bg-slate-900/40">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h4 className="font-semibold text-slate-900 dark:text-slate-100">
          {index + 1}. {name}
        </h4>
        {c && (
          <span className="text-xs text-slate-500 tabular-nums dark:text-slate-400" title="Worth (from step 2) × your match (from step 3)">
            earned {(c.weight * c.score).toFixed(2)} of {c.weight.toFixed(2)} points
          </span>
        )}
      </div>

      <div className="mt-2 space-y-2">
        <div className="rounded-md border border-slate-200 p-3 dark:border-slate-700">
          <StepLabel n={1} who="Haiku">Found in the listing</StepLabel>
          {listed?.listing_quote && (
            <blockquote className="mt-1.5 border-l-2 border-amber-400 pl-2 text-slate-700 italic dark:text-slate-300">
              "{listed.listing_quote}"
            </blockquote>
          )}
          <p className="mt-1.5 text-xs text-slate-500 dark:text-slate-400">
            Listed as <span className="font-medium text-slate-700 dark:text-slate-200">"{name}"</span>
            {listed?.generic && ' · generic, so worth a quarter'}
            {haiku && (
              <>
                {' · '}Haiku's own guess: {IMPORTANCE_LABELS[haiku] ?? haiku}
                {jevChoice && (jevChoice === haiku ? ' (same as Jev)' : ` (Jev chose ${IMPORTANCE_LABELS[jevChoice]}; Jev's is used)`)}
              </>
            )}
          </p>
        </div>

        <div className="grid gap-2 md:grid-cols-2">
          <QuestionBlock step={2} heading="How essential it is" q={listing?.questions[`importance_${index}`]} a={importance} />
          <QuestionBlock step={3} heading="How well you match it" q={resume?.questions[`match_${index}`]} a={resume?.answers[`match_${index}`]} />
        </div>
      </div>

      {flex?.noul !== undefined && (
        <p className="mt-2 text-xs text-slate-500 dark:text-slate-400">
          Accepts equivalents ("or similar"), also asked of Jev: {Math.round(flex.noul * 100)}% likely · counted as{' '}
          {flex.noul >= 0.5 ? 'yes' : 'no'}
        </p>
      )}
    </li>
  )
}

function StateView({ request }: { request: JevRequest }) {
  return (
    <div className="min-w-0 rounded-md border border-slate-200 p-3 dark:border-slate-700">
      <h5 className="text-xs font-semibold tracking-wide text-slate-500 uppercase dark:text-slate-400">{request.title}</h5>
      <dl className="mt-2 space-y-2">
        {Object.entries(request.state).map(([key, value]) => (
          <div key={key}>
            <dt className="text-xs font-medium text-slate-500 dark:text-slate-400">{key.replace(/_/g, ' ')}</dt>
            <dd className="max-h-48 overflow-auto rounded bg-slate-50 p-2 text-xs whitespace-pre-wrap text-slate-700 dark:bg-slate-900/40 dark:text-slate-300">
              {typeof value === 'string' ? value : JSON.stringify(value, null, 2)}
            </dd>
          </div>
        ))}
      </dl>
    </div>
  )
}

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="mt-4">
      <h4 className="mb-2 text-xs font-semibold tracking-wider text-slate-500 uppercase dark:text-slate-400">{title}</h4>
      {children}
    </section>
  )
}

// "What Jev was asked": loads on first open, then shows every question and answer per requirement.
export function JevPanel({ jobId, version, criteria }: { jobId: number; version: string; criteria: Criterion[] }) {
  const [data, setData] = useState<JevRequestsResponse | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)

  function load() {
    if (data || loading) return
    setLoading(true)
    fetch(`/api/jev-requests/${jobId}?version=${encodeURIComponent(version)}`)
      .then((res) => (res.ok ? res.json() : Promise.reject(new Error(`The API returned ${res.status}`))))
      .then((body: JevRequestsResponse) => setData(body))
      .catch((e: Error) => setError(`Couldn't load Jev's questions: ${e.message}`))
      .finally(() => setLoading(false))
  }

  const listing = data?.requests.find((r) => r.request_no === 1)
  const resume = data?.requests.find((r) => r.request_no === 2)
  const reqs = data?.requirements

  return (
    <details className="mt-2" onToggle={(e) => (e.currentTarget as HTMLDetailsElement).open && load()}>
      <summary className="cursor-pointer text-slate-500 hover:text-slate-800 dark:text-slate-400 dark:hover:text-slate-200">
        How this job was rated, step by step
      </summary>

      {loading && <p className="mt-2 text-slate-500">Loading…</p>}
      {error && <p className="mt-2 rounded-md bg-red-50 p-2 text-red-700 dark:bg-red-950 dark:text-red-200">{error}</p>}

      {data && reqs && (
        <div className="mt-2">
          <ol className="space-y-1 text-slate-600 dark:text-slate-300">
            <li>
              <span className="font-semibold">1.</span> {reqs.source.startsWith('Haiku') ? 'Haiku' : 'Sonnet'} read the
              listing and listed {reqs.items.length} requirements
              {reqs.cost_usd !== null && ` ($${reqs.cost_usd.toFixed(4)})`}.
            </li>
            <li>
              <span className="font-semibold">2.</span> Jev read the listing and judged how essential each one is.
            </li>
            <li>
              <span className="font-semibold">3.</span> Jev read your resume and judged how well you match each one
              {listing?.model && ` (${listing.model}`}
              {listing?.input_tokens && resume?.input_tokens
                ? `, ${(listing.input_tokens + resume.input_tokens).toLocaleString()} tokens)`
                : listing?.model
                  ? ')'
                  : ''}
              .
            </li>
            <li>
              <span className="font-semibold">4.</span> Code adds up the points and applies the caps.
            </li>
          </ol>

          {reqs.instructions && (
            <details className="mt-2">
              <summary className="cursor-pointer text-xs text-slate-500 hover:text-slate-800 dark:text-slate-400 dark:hover:text-slate-200">
                Show exactly what Haiku was told
              </summary>
              <pre className="mt-1 max-h-72 overflow-auto rounded-md bg-slate-50 p-3 text-xs whitespace-pre-wrap text-slate-700 dark:bg-slate-900/40 dark:text-slate-300">
                {reqs.instructions}
              </pre>
            </details>
          )}

          <Section title="Requirement by requirement">
            <ol className="space-y-3">
              {criteria.map((c, i) => (
                <RequirementCard key={i} index={i} c={c} listed={reqs.items[i]} listing={listing} resume={resume} />
              ))}
            </ol>
          </Section>

          {listing?.questions.clearance && (
            <Section title="Whole job">
              <QuestionBlock heading="Clearance or citizenship (Jev)" q={listing.questions.clearance} a={listing.answers.clearance} />
            </Section>
          )}

          <Section title="What Jev read">
            <details>
              <summary className="cursor-pointer text-slate-500 hover:text-slate-800 dark:text-slate-400 dark:hover:text-slate-200">
                Show the listing and resume exactly as sent
              </summary>
              <div className="mt-2 grid gap-2 md:grid-cols-2">
                {data.requests.map((r) => (
                  <StateView key={r.request_no} request={r} />
                ))}
              </div>
            </details>
          </Section>
        </div>
      )}
    </details>
  )
}
