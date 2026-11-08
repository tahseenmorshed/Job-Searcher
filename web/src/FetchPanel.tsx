// Fetch new listings, then triage them, list requirements and rate them. Pick cities and sources
// (each with its own timeframe), see what the fetch is likely to cost, and start it. Each AI step
// then shows its own estimate and waits for Continue or Stop, unless auto-approve is on. A log
// window shows what it's doing, line by line. The work runs on the API (pipeline.py), so closing
// or reloading the page doesn't stop it; the page picks it up again when it reopens.
import { useEffect, useRef, useState, type ReactNode } from 'react'
import { api } from './api'
import type { Approval, FetchEstimate, FetchOptions, FetchParams, FetchSource, PipelineParams, Scrape, Task, TaskStep } from './types'
import { active, type FetchTask } from './useFetchTask'

const SOURCES: FetchSource[] = ['seek', 'linkedin']

function money(usd: number | null | undefined): string {
  if (usd === null || usd === undefined) return '–'
  return usd >= 1 ? `$${usd.toFixed(2)}` : `$${usd.toFixed(3)}`
}

function timeframe(days: number): string {
  return days === 1 ? 'past 24 hours' : `past ${days} days`
}

// '2026-11-05T15:04:24' -> 'today 15:04', 'yesterday 01:34' or '3 Nov 01:34'
function when(value: string): string {
  const date = new Date(value)
  const time = value.slice(11, 16)
  const today = new Date()
  const yesterday = new Date(today.getTime() - 86_400_000)
  if (date.toDateString() === today.toDateString()) return `today ${time}`
  if (date.toDateString() === yesterday.toDateString()) return `yesterday ${time}`
  return `${date.toLocaleDateString('en-AU', { day: 'numeric', month: 'short' })} ${time}`
}

// Seconds since a timestamp, as '2m 05s'.
function elapsed(since: string | null, now: number): string {
  if (!since) return ''
  const s = Math.max(0, Math.round((now - new Date(since).getTime()) / 1000))
  return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, '0')}s`
}

export function FetchButton({
  open,
  running,
  waiting,
  onClick,
}: {
  open: boolean
  running: boolean
  waiting: boolean
  onClick: () => void
}) {
  return (
    <button
      onClick={onClick}
      aria-expanded={open}
      title="Fetch new listings from Seek and LinkedIn"
      className={`flex items-center gap-2 rounded-md border px-2 py-1 ${
        open
          ? 'border-sky-600 bg-sky-50 text-sky-800 dark:bg-sky-900/40 dark:text-sky-200'
          : 'border-slate-300 bg-white hover:border-slate-400 dark:border-slate-600 dark:bg-slate-800'
      }`}
    >
      {running && !waiting && <Spinner />}
      {waiting && <span aria-hidden className="size-2 rounded-full bg-amber-500" />}
      {waiting ? 'Needs approval' : running ? 'Working…' : 'Fetch listings'}
    </button>
  )
}

export function FetchPanel({ fetchTask, reloads }: { fetchTask: FetchTask; reloads: number }) {
  const [options, setOptions] = useState<FetchOptions | null>(null)
  const [cities, setCities] = useState<string[]>([])
  // Each source's timeframe, kept while it's switched off so switching it back on restores it.
  const [days, setDays] = useState<Record<FetchSource, number>>({ seek: 3, linkedin: 1 })
  const [picked, setPicked] = useState<FetchSource[]>([])
  const [estimate, setEstimate] = useState<FetchEstimate | null>(null)
  const [scrapes, setScrapes] = useState<Scrape[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [starting, setStarting] = useState(false)
  const [process, setProcess] = useState(true) // triage, list requirements and rate after fetching
  const [autoApprove, setAutoApprove] = useState(false)

  useEffect(() => {
    api
      .taskOptions()
      .then(({ fetch: o }) => {
        setOptions(o)
        setCities(o.defaults.cities)
        setDays({ seek: o.sources.seek.default_days, linkedin: o.sources.linkedin.default_days })
        setPicked(SOURCES.filter((s) => o.defaults[s] !== null))
      })
      .catch((e) => setError(e.message))
  }, [])

  // Loaded when the panel opens and again after each fetch.
  useEffect(() => {
    api
      .scrapes()
      .then(setScrapes)
      .catch(() => setScrapes(null))
  }, [reloads])

  const params: FetchParams = {
    cities,
    seek: picked.includes('seek') ? { days: days.seek } : null,
    linkedin: picked.includes('linkedin') ? { days: days.linkedin } : null,
  }
  const ready = options !== null && cities.length > 0 && picked.length > 0

  // The estimate is free, so it's refreshed whenever the choices change.
  const key = JSON.stringify(params)
  useEffect(() => {
    if (!ready) return setEstimate(null)
    let current = true
    api
      .estimate({ fetch: params, process: false, auto_approve: false })
      .then((e) => current && setEstimate(e))
      .catch(() => current && setEstimate(null))
    return () => {
      current = false
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, ready])

  // fetchToo false: skip the fetch and only process jobs already saved.
  async function start(fetchToo: boolean) {
    setError(null)
    setStarting(true)
    const run: PipelineParams = { fetch: fetchToo ? params : null, process: process || !fetchToo, auto_approve: autoApprove }
    try {
      await fetchTask.start(run)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setStarting(false)
    }
  }

  const { task, running } = fetchTask
  const toggle = <T,>(list: T[], item: T) => (list.includes(item) ? list.filter((x) => x !== item) : [...list, item])

  return (
    <section className="mt-6 rounded-lg border border-slate-200 bg-white p-4 text-sm dark:border-slate-700 dark:bg-slate-800">
      <Heading>Fetch listings</Heading>

      {options && (
        <fieldset disabled={running} className="space-y-3 disabled:opacity-60">
          <Row label="Cities">
            {options.cities.map((city) => (
              <Chip key={city} selected={cities.includes(city)} onClick={() => setCities(toggle(cities, city))}>
                {city}
              </Chip>
            ))}
          </Row>
          <Row label="Sources">
            {SOURCES.map((source) => {
              const selected = picked.includes(source)
              const { label, days: choices } = options.sources[source]
              return (
                <span key={source} className="flex items-center gap-1.5">
                  <Chip selected={selected} onClick={() => setPicked(toggle(picked, source))}>
                    {label}
                  </Chip>
                  {selected && (
                    <select
                      value={days[source]}
                      onChange={(e) => setDays({ ...days, [source]: Number(e.target.value) })}
                      aria-label={`${label} timeframe`}
                      className="rounded-md border border-slate-300 bg-white px-2 py-1 dark:border-slate-600 dark:bg-slate-800"
                    >
                      {choices.map((d) => (
                        <option key={d} value={d}>
                          {timeframe(d)}
                        </option>
                      ))}
                    </select>
                  )}
                </span>
              )
            })}
          </Row>
          <Row label="Then">
            <label className="flex items-center gap-1.5">
              <input type="checkbox" checked={process} onChange={(e) => setProcess(e.target.checked)} />
              Triage, list requirements and rate new jobs
            </label>
            {process && (
              <label className="flex items-center gap-1.5" title="Each AI step normally shows its cost and waits for you">
                <input type="checkbox" checked={autoApprove} onChange={(e) => setAutoApprove(e.target.checked)} />
                Don't ask before each step
              </label>
            )}
          </Row>
        </fieldset>
      )}

      {!running && options && (
        <div className="mt-4 flex flex-wrap items-start gap-3">
          <button
            onClick={() => start(true)}
            disabled={!ready || starting}
            className="rounded-md bg-sky-600 px-3 py-1.5 font-medium text-white hover:bg-sky-700 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {starting ? 'Starting…' : 'Fetch now'}
          </button>
          <button
            onClick={() => start(false)}
            disabled={starting}
            title="Triage, list requirements and rate jobs already saved that haven't been done yet. No fetch."
            className="rounded-md border border-slate-300 px-3 py-1.5 hover:border-slate-400 disabled:opacity-50 dark:border-slate-600"
          >
            Process saved jobs
          </button>
          <div className="min-w-0 flex-1 pt-1.5 text-slate-500 dark:text-slate-400">
            {cities.length === 0 ? (
              'Pick at least one city.'
            ) : picked.length === 0 ? (
              'Pick at least one source.'
            ) : (
              <>
                {estimate && <EstimateText estimate={estimate} options={options} cities={cities.length} />}
                {process && (
                  <span className="mt-1 block">
                    {autoApprove
                      ? 'The AI steps will run without asking.'
                      : 'Each AI step shows its cost and waits for you before spending.'}
                  </span>
                )}
              </>
            )}
          </div>
        </div>
      )}

      {error && <p className="mt-3 rounded-md bg-red-50 p-2 text-red-700 dark:bg-red-950/40 dark:text-red-300">{error}</p>}

      {task && (
        <TaskStatus task={task} onAnswer={fetchTask.answer} onAbort={fetchTask.abort} onDismiss={fetchTask.dismiss} />
      )}
      {task && <LogWindow key={task.id} task={task} />}

      {options && scrapes && <LastFetched scrapes={scrapes} options={options} cities={cities} picked={picked} />}
    </section>
  )
}

function EstimateText({ estimate, options, cities }: { estimate: FetchEstimate; options: FetchOptions; cities: number }) {
  const parts = SOURCES.flatMap((s) => {
    const e = estimate.by_source[s]
    if (!e) return []
    const label = options.sources[s].label
    if (e.estimate_usd === null) return [`${label}: no past fetches to go by`]
    const perCity = s === 'linkedin' && cities > 1 ? ` for ${cities} cities` : ''
    return [`${label} about ${money(e.estimate_usd)}${perCity} (${money(e.low_usd)}–${money(e.high_usd)})`]
  })
  const further = SOURCES.flatMap((s) => {
    const e = estimate.by_source[s]
    return e?.longer_than_before ? [`${options.sources[s].label} (longest so far: ${timeframe(e.longest_searched_days!)})`] : []
  })
  return (
    <span title="Apify charges by what it scrapes, so the cost is only known afterwards. These are from recent fetches.">
      {estimate.total_usd !== null && <>Likely about {money(estimate.total_usd)}. </>}
      {parts.join(' · ')}.
      {further.length > 0 && (
        <span className="mt-1 block text-amber-700 dark:text-amber-400">
          Searching further back than any past fetch from {further.join(' and ')}, so it may cost more.
        </span>
      )}
    </span>
  )
}

const STEP_ICON: Record<TaskStep['status'], string> = {
  pending: '○',
  running: '◌',
  waiting: '?',
  done: '✓',
  skipped: '–',
  stopped: '■',
  failed: '✕',
}
const STEP_TONE: Record<TaskStep['status'], string> = {
  pending: 'text-slate-400',
  running: 'text-sky-600 dark:text-sky-400',
  waiting: 'text-amber-600 dark:text-amber-400',
  done: 'text-emerald-600 dark:text-emerald-400',
  skipped: 'text-slate-400',
  stopped: 'text-slate-500',
  failed: 'text-red-600 dark:text-red-400',
}

function TaskStatus({
  task,
  onAnswer,
  onAbort,
  onDismiss,
}: {
  task: Task
  onAnswer: (approve: boolean) => Promise<void>
  onAbort: () => Promise<void>
  onDismiss: () => void
}) {
  const [now, setNow] = useState(() => Date.now())
  const running = active(task)
  useEffect(() => {
    if (!running) return
    const timer = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(timer)
  }, [running])

  const fetched = task.result?.fetch
  const failedSources = fetched ? Object.values(fetched.by_source).filter((s) => !s.ok) : []
  const tone =
    task.status === 'failed'
      ? 'border-red-200 dark:border-red-900'
      : task.status === 'waiting'
        ? 'border-amber-300 dark:border-amber-700'
        : 'border-slate-200 dark:border-slate-700'

  return (
    <div className={`mt-4 rounded-md border p-3 ${tone}`}>
      <div className="flex items-start justify-between gap-3">
        <ol className="min-w-0 space-y-1">
          {task.steps.map((s) => (
            <li key={s.key} className="flex gap-2">
              <span className={`w-4 shrink-0 text-center ${STEP_TONE[s.status]}`}>
                {s.status === 'running' ? <Spinner /> : STEP_ICON[s.status]}
              </span>
              <span className="font-medium">{s.label}</span>
              {s.detail && <span className="min-w-0 truncate text-slate-500 dark:text-slate-400">{s.detail}</span>}
            </li>
          ))}
        </ol>
        {running ? (
          <button
            onClick={onAbort}
            disabled={task.abort_requested}
            title="Stops after the job in progress. Finished jobs are kept, and the rest are picked up next run."
            className="shrink-0 rounded-md border border-red-300 px-2 py-0.5 text-xs text-red-700 hover:bg-red-50 disabled:opacity-60 dark:border-red-800 dark:text-red-300 dark:hover:bg-red-950/40"
          >
            {task.abort_requested ? 'Stopping…' : 'Abort'}
          </button>
        ) : (
          <button onClick={onDismiss} className="shrink-0 text-xs opacity-70 hover:opacity-100">
            Dismiss
          </button>
        )}
      </div>

      {task.approval && <ApprovalCard approval={task.approval} onAnswer={onAnswer} />}

      {running && !task.approval && (
        <p className="mt-2 text-xs text-slate-500 dark:text-slate-400">
          {task.abort_requested
            ? task.steps.find((s) => s.status === 'running')?.key === 'fetch'
              ? "Stopping once the fetch finishes: a scrape can't be cut short, and its jobs will still be saved."
              : 'Stopping after the job in progress…'
            : `${elapsed(task.started_at ?? task.created_at, now)} so far. You can close this page; it keeps going.`}
        </p>
      )}
      {failedSources.map((s) => !s.ok && (
        <p key={s.error} className="mt-2 text-red-700 dark:text-red-300">A source failed: {s.error}</p>
      ))}
      {task.status === 'failed' && <p className="mt-2 text-red-700 dark:text-red-300">Failed: {task.error}</p>}
      {task.status === 'done' &&
        (task.result?.aborted ? (
          <p className="mt-2 text-slate-600 dark:text-slate-300">
            Aborted. Jobs finished before that are saved and shown; the rest are picked up next run.
          </p>
        ) : (
          <p className="mt-2 text-emerald-700 dark:text-emerald-400">Done. The job list has been refreshed.</p>
        ))}
    </div>
  )
}

// A paid step waiting for the go-ahead: what it will do, what it should cost, Continue or Stop.
function ApprovalCard({ approval: a, onAnswer }: { approval: Approval; onAnswer: (approve: boolean) => Promise<void> }) {
  const [sending, setSending] = useState(false)
  const answer = async (approve: boolean) => {
    setSending(true)
    try {
      await onAnswer(approve)
    } finally {
      setSending(false)
    }
  }
  return (
    <div className="mt-3 rounded-md bg-amber-50 p-3 text-amber-950 dark:bg-amber-900/30 dark:text-amber-100">
      <p className="font-medium">
        {a.label}: {a.jobs} {a.jobs === 1 ? 'job' : 'jobs'} with {a.model}, estimated {money(a.estimate_usd)}
      </p>
      <p className="mt-0.5 text-xs opacity-80">
        ~{a.input_tokens.toLocaleString()} input tokens · {a.version}
        {a.note && ` · ${a.note}`}
      </p>
      {a.warning && <p className="mt-2 text-xs font-medium text-red-700 dark:text-red-300">Warning: {a.warning}</p>}
      <div className="mt-3 flex gap-2">
        <button
          onClick={() => answer(true)}
          disabled={sending}
          className="rounded-md bg-sky-600 px-3 py-1 font-medium text-white hover:bg-sky-700 disabled:opacity-50"
        >
          Continue
        </button>
        <button
          onClick={() => answer(false)}
          disabled={sending}
          className="rounded-md border border-amber-400 px-3 py-1 hover:bg-amber-100 disabled:opacity-50 dark:border-amber-700 dark:hover:bg-amber-900/50"
        >
          Stop here
        </button>
      </div>
    </div>
  )
}

// What the task is doing, line by line, as the scripts print it. Kept by the API in memory only.
// Keyed by task id, so a new task starts with an empty window.
function LogWindow({ task }: { task: Task }) {
  const [lines, setLines] = useState<string[]>([])
  const [open, setOpen] = useState(true)
  const next = useRef(0)
  const box = useRef<HTMLPreElement>(null)
  const running = active(task)

  // Fetch new lines while it runs, and once more when it stops.
  useEffect(() => {
    let cancelled = false
    const load = () =>
      api
        .taskLog(task.id, next.current)
        .then(({ lines: more, next: n }) => {
          if (cancelled || more.length === 0) return
          next.current = n
          setLines((old) => [...old, ...more].slice(-2000))
        })
        .catch(() => {})
    load()
    if (!running) return
    const timer = setInterval(load, 1000)
    return () => {
      cancelled = true
      clearInterval(timer)
    }
  }, [task.id, running])

  // Keep the newest line in view, unless you've scrolled up to read.
  useEffect(() => {
    const el = box.current
    if (el && el.scrollHeight - el.scrollTop - el.clientHeight < 60) el.scrollTop = el.scrollHeight
  }, [lines])

  return (
    <div className="mt-3">
      <button onClick={() => setOpen(!open)} className="text-xs text-slate-500 hover:underline dark:text-slate-400">
        {open ? 'Hide log' : 'Show log'} ({lines.length} lines)
      </button>
      {open && (
        <pre
          ref={box}
          className="mt-1 h-56 overflow-auto rounded-md bg-slate-950 p-2 font-mono text-xs leading-5 whitespace-pre-wrap text-slate-200"
        >
          {lines.length ? lines.join('\n') : 'Waiting for output…'}
        </pre>
      )}
    </div>
  )
}

// When each city was last fetched from each source. Cities and sources not picked are dimmed.
function LastFetched({
  scrapes,
  options,
  cities,
  picked,
}: {
  scrapes: Scrape[]
  options: FetchOptions
  cities: string[]
  picked: FetchSource[]
}) {
  const find = (source: FetchSource, city: string) => scrapes.find((s) => s.source === source && s.city === city)
  return (
    <div className="mt-5">
      <Heading>Last fetched</Heading>
      <div className="overflow-x-auto">
        <table className="w-full text-left">
          <thead className="text-xs text-slate-500 dark:text-slate-400">
            <tr>
              <th className="py-1 pr-3 font-medium" />
              {SOURCES.map((s) => (
                <th key={s} className={`py-1 pr-3 font-medium ${picked.includes(s) ? '' : 'opacity-50'}`}>
                  {options.sources[s].label}
                </th>
              ))}
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-100 dark:divide-slate-700">
            {options.cities.map((city) => (
              <tr key={city} className={cities.includes(city) ? '' : 'opacity-50'}>
                <th className="py-1.5 pr-3 font-medium">{city}</th>
                {SOURCES.map((source) => {
                  const s = find(source, city)
                  return (
                    <td
                      key={source}
                      className={`py-1.5 pr-3 tabular-nums ${picked.includes(source) ? '' : 'opacity-50'}`}
                      title={s ? `${s.scraped_at}: searched the ${s.days ? timeframe(s.days) : 'unknown timeframe'}` : undefined}
                    >
                      {s ? (
                        <>
                          {when(s.scraped_at)}{' '}
                          <span className="text-slate-500 dark:text-slate-400">
                            · {s.days ? timeframe(s.days) : '?'} · {s.jobs_found} {s.jobs_found === 1 ? 'job' : 'jobs'}
                          </span>
                        </>
                      ) : (
                        <span className="text-slate-400">Never</span>
                      )}
                    </td>
                  )
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}

function Heading({ children }: { children: ReactNode }) {
  return (
    <h3 className="mb-3 text-xs font-semibold tracking-wider text-slate-500 uppercase dark:text-slate-400">{children}</h3>
  )
}

function Row({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flex flex-wrap items-center gap-2">
      <span className="w-16 shrink-0 text-slate-500 dark:text-slate-400">{label}</span>
      {children}
    </div>
  )
}

function Chip({ selected, onClick, children }: { selected: boolean; onClick: () => void; children: ReactNode }) {
  return (
    <button
      aria-pressed={selected}
      onClick={onClick}
      className={`rounded-full border px-3 py-1 ${
        selected
          ? 'border-sky-600 bg-sky-600 text-white'
          : 'border-slate-300 bg-white text-slate-700 hover:border-slate-400 dark:border-slate-600 dark:bg-slate-800 dark:text-slate-200'
      }`}
    >
      {children}
    </button>
  )
}

function Spinner() {
  return (
    <span
      aria-hidden
      className="mt-0.5 inline-block size-3.5 shrink-0 animate-spin rounded-full border-2 border-current border-r-transparent"
    />
  )
}
