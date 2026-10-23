import { useEffect, useState, type ReactNode } from 'react'
import { KeptCard, RejectedRow } from './JobCard'
import { REJECTED_GROUPS, type Job, type JobsResponse } from './types'

type Tab = 'core' | 'adjacent' | 'rejected'

const DAY_OPTIONS = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]

// True if the job was posted within the last `days` days. Jobs without a posted date are left out.
function postedWithin(job: Job, days: number): boolean {
  if (!job.posted_at) return false
  const posted = new Date(job.posted_at).getTime()
  return Date.now() - posted <= days * 24 * 60 * 60 * 1000
}

// Seek locations are 'Melbourne VIC' or 'Richmond, Melbourne VIC': the city is the part after the last comma.
function city(job: Job): string {
  return job.location?.split(',').pop()?.trim() || 'Unknown'
}

function App() {
  const [versions, setVersions] = useState<string[]>([])
  const [version, setVersion] = useState<string>('') // '' = let the API pick the latest
  const [assessVersions, setAssessVersions] = useState<string[]>([])
  const [assessVersion, setAssessVersion] = useState<string>('') // '' = let the API pick the latest
  const [days, setDays] = useState<number | null>(null) // null = any time
  const [cities, setCities] = useState<string[]>([]) // empty = all cities
  const [tab, setTab] = useState<Tab>('core')
  const [data, setData] = useState<JobsResponse | null>(null)
  const [error, setError] = useState<string | null>(null)

  // Load the list of prompt versions once, when the page opens.
  useEffect(() => {
    fetch('/api/versions')
      .then((res) => res.json())
      .then(setVersions)
      .catch(() => setError('Could not reach the API. Is uvicorn running?'))
    fetch('/api/assess-versions')
      .then((res) => res.json())
      .then(setAssessVersions)
      .catch(() => setError('Could not reach the API. Is uvicorn running?'))
  }, [])

  // Reload jobs whenever the triage or assessment version changes.
  useEffect(() => {
    const params = new URLSearchParams()
    if (version) params.set('version', version)
    if (assessVersion) params.set('assess_version', assessVersion)
    fetch(`/api/jobs?${params}`)
      .then((res) => res.json())
      .then((body: JobsResponse) => {
        setData(body)
        setError(null)
      })
      .catch(() => setError('Could not reach the API. Is uvicorn running?'))
  }, [version, assessVersion])

  // Filters run here in the browser: all jobs are already loaded, so changing them is instant.
  const inDateRange = (data?.jobs ?? []).filter((j) => days === null || postedWithin(j, days))

  // City options come from the jobs themselves, so a new city (e.g. Sydney) shows up on its own.
  // Counts respect the date filter but not the city filter, so you can see what each choice would add.
  const cityCounts = new Map<string, number>()
  for (const c of cities) cityCounts.set(c, 0) // keep selected chips visible even if the date filter empties them
  for (const j of inDateRange) cityCounts.set(city(j), (cityCounts.get(city(j)) ?? 0) + 1)
  const cityOptions = [...cityCounts.entries()].sort((a, b) => b[1] - a[1])

  const jobs = inDateRange.filter((j) => cities.length === 0 || cities.includes(city(j)))
  // Assessed core jobs first, best score first; unassessed ones keep their posted order after them.
  const core = jobs
    .filter((j) => j.decision === 'keep' && j.reason === 'core')
    .sort((a, b) => (b.assessment?.score ?? -1) - (a.assessment?.score ?? -1))
  const adjacent = jobs.filter((j) => j.decision === 'keep' && j.reason === 'adjacent')
  const openBackground = jobs.filter((j) => j.decision === 'keep' && j.reason === 'open_background')
  const rejected = jobs.filter((j) => j.decision === 'reject')
  const tabCount = { core: core.length, adjacent: adjacent.length + openBackground.length, rejected: rejected.length }

  return (
    <div className="min-h-screen bg-slate-50 text-slate-900 dark:bg-slate-900 dark:text-slate-100">
      <div className="mx-auto max-w-3xl px-4 py-8">
        <header className="flex flex-wrap items-center justify-between gap-4">
          <h1 className="text-2xl font-bold">JobSeeker</h1>
          <div className="flex flex-wrap items-center gap-3 text-sm">
            <select
              value={version || data?.version || ''}
              onChange={(e) => setVersion(e.target.value)}
              className="rounded-md border border-slate-300 bg-white px-2 py-1 dark:border-slate-600 dark:bg-slate-800"
            >
              {versions.map((v) => (
                <option key={v} value={v}>
                  {v}
                </option>
              ))}
            </select>
            {assessVersions.length > 0 && (
              <select
                value={assessVersion || data?.assess_version || ''}
                onChange={(e) => setAssessVersion(e.target.value)}
                title="Assessment version (which model's ratings to show)"
                className="rounded-md border border-slate-300 bg-white px-2 py-1 dark:border-slate-600 dark:bg-slate-800"
              >
                {assessVersions.map((v) => (
                  <option key={v} value={v}>
                    {v}
                  </option>
                ))}
              </select>
            )}
            <select
              value={days ?? ''}
              onChange={(e) => setDays(e.target.value ? Number(e.target.value) : null)}
              className="rounded-md border border-slate-300 bg-white px-2 py-1 dark:border-slate-600 dark:bg-slate-800"
            >
              <option value="">Posted any time</option>
              {DAY_OPTIONS.map((d) => (
                <option key={d} value={d}>
                  Posted in last {d} {d === 1 ? 'day' : 'days'}
                </option>
              ))}
            </select>
          </div>
        </header>

        <div className="mt-4 flex flex-wrap items-center gap-2 text-sm">
          {cityOptions.map(([name, count]) => {
            const selected = cities.includes(name)
            return (
              <button
                key={name}
                onClick={() => setCities(selected ? cities.filter((c) => c !== name) : [...cities, name])}
                className={`rounded-full border px-3 py-1 ${
                  selected
                    ? 'border-sky-600 bg-sky-600 text-white'
                    : 'border-slate-300 bg-white text-slate-700 hover:border-slate-400 dark:border-slate-600 dark:bg-slate-800 dark:text-slate-200'
                }`}
              >
                {name} <span className={selected ? 'text-sky-100' : 'text-slate-400'}>{count}</span>
              </button>
            )
          })}
          {cities.length > 0 && (
            <button onClick={() => setCities([])} className="text-slate-500 hover:underline">
              Clear
            </button>
          )}
        </div>

        {error && <p className="mt-6 rounded-md bg-red-50 p-3 text-sm text-red-700">{error}</p>}

        <nav className="mt-6 flex gap-1 border-b border-slate-200 dark:border-slate-700">
          <TabButton active={tab === 'core'} onClick={() => setTab('core')}>
            Core <Count n={core.length} />
          </TabButton>
          <TabButton active={tab === 'adjacent'} onClick={() => setTab('adjacent')}>
            Adjacent <Count n={tabCount.adjacent} />
          </TabButton>
          <TabButton active={tab === 'rejected'} onClick={() => setTab('rejected')}>
            Rejected <Count n={rejected.length} />
          </TabButton>
        </nav>

        {tab === 'core' && <CardList jobs={core} />}

        {tab === 'adjacent' && (
          <>
            <CardList jobs={adjacent} />
            <Group label="Open to software background" count={openBackground.length}>
              <CardList jobs={openBackground} />
            </Group>
          </>
        )}

        {tab === 'rejected' &&
          REJECTED_GROUPS.map(({ reason, label }) => {
            const group = rejected.filter((j) => j.reason === reason)
            return (
              <Group key={reason} label={label} count={group.length}>
                <ul className="divide-y divide-slate-200 rounded-lg border border-slate-200 bg-white px-4 dark:divide-slate-700 dark:border-slate-700 dark:bg-slate-800">
                  {group.map((job) => (
                    <RejectedRow key={job.id} job={job} />
                  ))}
                </ul>
              </Group>
            )
          })}

        {data && tabCount[tab] === 0 && (
          <p className="mt-8 text-center text-slate-500">
            No jobs here{days !== null ? ` posted in the last ${days} ${days === 1 ? 'day' : 'days'}` : ''}.
          </p>
        )}
      </div>
    </div>
  )
}

function CardList({ jobs }: { jobs: Job[] }) {
  if (jobs.length === 0) return null
  return (
    <div className="mt-6 space-y-3">
      {jobs.map((job) => (
        <KeptCard key={job.id} job={job} />
      ))}
    </div>
  )
}

// A group heading with its jobs underneath; hidden when the group is empty.
function Group({ label, count, children }: { label: string; count: number; children: ReactNode }) {
  if (count === 0) return null
  return (
    <section className="mt-8">
      <h2 className="mb-3 text-xs font-semibold tracking-wider text-slate-500 uppercase dark:text-slate-400">
        {label} ({count})
      </h2>
      {children}
    </section>
  )
}

function TabButton({ active, onClick, children }: { active: boolean; onClick: () => void; children: ReactNode }) {
  return (
    <button
      onClick={onClick}
      className={`-mb-px border-b-2 px-4 py-2 text-sm font-medium ${
        active
          ? 'border-sky-600 text-sky-700 dark:text-sky-400'
          : 'border-transparent text-slate-500 hover:text-slate-800 dark:hover:text-slate-200'
      }`}
    >
      {children}
    </button>
  )
}

function Count({ n }: { n: number }) {
  return <span className="ml-1 rounded-full bg-slate-100 px-2 py-0.5 text-xs dark:bg-slate-800">{n}</span>
}

export default App
