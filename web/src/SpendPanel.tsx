import { useState } from "react";
import type { SourceSpend, SpendRun, SpendSummary } from "./types";

// Small amounts need more digits to mean anything: $0.0123, but $2.26.
function money(usd: number | null): string {
  if (usd === null) return "–";
  return usd >= 1 ? `$${usd.toFixed(2)}` : `$${usd.toFixed(4)}`;
}

// '2026-10-27T19:18:02' -> '27 Oct 19:18'
function when(value: string): string {
  const date = new Date(value);
  return `${date.toLocaleDateString("en-AU", { day: "numeric", month: "short" })} ${value.slice(11, 16)}`;
}

function monthName(month: string): string {
  return new Date(`${month}-01T00:00:00`).toLocaleDateString("en-AU", {
    month: "long",
    year: "numeric",
  });
}

const STATUS_NOTE: Record<SpendRun["status"], string> = {
  done: "",
  running: "running, or stopped without finishing",
  aborted: "stopped part way",
  backfilled: "rebuilt from saved results",
};

export function SpendPanel({ data }: { data: SpendSummary }) {
  const [showAll, setShowAll] = useState(false);
  const largest = Math.max(...data.by_step.map((s) => s.cost_usd), 0);
  const runs = showAll ? data.recent : data.recent.slice(0, 5);

  return (
    <section className="mt-6 rounded-lg border border-slate-200 bg-white p-4 dark:border-slate-700 dark:bg-slate-800">
      <div className="flex flex-wrap gap-8">
        <Stat label="Spent in total" value={money(data.total_usd)} />
        <Stat
          label={`Spent in ${monthName(data.month)}`}
          value={money(data.this_month_usd)}
        />
      </div>

      <h3 className="mt-5 mb-2 text-xs font-semibold tracking-wider text-slate-500 uppercase dark:text-slate-400">
        By step
      </h3>
      <ul className="space-y-1.5 text-sm">
        {data.by_step.map((s) => (
          <li
            key={s.step}
            className="grid grid-cols-[minmax(0,11rem)_1fr_4.5rem] items-center gap-3"
            title={`${s.label}: ${money(s.cost_usd)} over ${s.runs} runs${s.items ? `, ${s.items} jobs` : ""}. Last run ${when(s.last_run)}`}
          >
            <span className="truncate text-slate-700 dark:text-slate-200">
              {s.label}
            </span>
            <span className="h-2.5 rounded-r bg-slate-100 dark:bg-slate-700">
              <span
                className="block h-full rounded-r bg-sky-600 dark:bg-sky-400"
                style={{
                  width: `${largest ? Math.max((s.cost_usd / largest) * 100, 1) : 0}%`,
                }}
              />
            </span>
            <span className="text-right text-slate-900 tabular-nums dark:text-slate-100">
              {money(s.cost_usd)}
            </span>
          </li>
        ))}
      </ul>

      {data.by_source.length > 0 && (
        <>
          <h3 className="mt-5 mb-2 text-xs font-semibold tracking-wider text-slate-500 uppercase dark:text-slate-400">
            By source
          </h3>
          <p className="mb-2 text-xs text-slate-500 dark:text-slate-400">
            Each site's scraping, plus the AI steps run on the jobs it brought in.
          </p>
          <ul className="space-y-3 text-sm">
            {data.by_source.map((s) => (
              <SourceRow key={s.source} data={s} />
            ))}
          </ul>
        </>
      )}

      {data.by_month.length > 1 && (
        <>
          <h3 className="mt-5 mb-2 text-xs font-semibold tracking-wider text-slate-500 uppercase dark:text-slate-400">
            By month
          </h3>
          <ul className="space-y-1 text-sm">
            {data.by_month.map((m) => (
              <li key={m.month} className="flex justify-between">
                <span className="text-slate-700 dark:text-slate-200">
                  {monthName(m.month)}
                </span>
                <span className="tabular-nums">{money(m.cost_usd)}</span>
              </li>
            ))}
          </ul>
        </>
      )}

      <h3 className="mt-5 mb-2 text-xs font-semibold tracking-wider text-slate-500 uppercase dark:text-slate-400">
        Latest runs
      </h3>
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead className="text-left text-xs text-slate-500 dark:text-slate-400">
            <tr>
              <th className="py-1 pr-3 font-medium">Started</th>
              <th className="py-1 pr-3 font-medium">Step</th>
              <th className="py-1 pr-3 text-right font-medium">Jobs</th>
              <th className="py-1 pr-3 text-right font-medium">Estimate</th>
              <th className="py-1 text-right font-medium">Actual</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-100 dark:divide-slate-700">
            {runs.map((r) => (
              <tr key={r.id} title={STATUS_NOTE[r.status] || undefined}>
                <td className="py-1.5 pr-3 whitespace-nowrap text-slate-500 dark:text-slate-400">
                  {when(r.started_at)}
                </td>
                <td className="py-1.5 pr-3">
                  {r.label}
                  {r.version && (
                    <span className="ml-1.5 text-xs text-slate-500 dark:text-slate-400">
                      {r.version}
                    </span>
                  )}
                  {r.status !== "done" && r.status !== "backfilled" && (
                    <span className="ml-1.5 rounded bg-amber-100 px-1.5 py-0.5 text-xs text-amber-800 dark:bg-amber-900/40 dark:text-amber-200">
                      {r.status}
                    </span>
                  )}
                </td>
                <td className="py-1.5 pr-3 text-right tabular-nums">
                  {r.items_ok || r.items_failed ? r.items_ok : "–"}
                  {r.items_failed > 0 && (
                    <span className="ml-1 text-xs text-red-700 dark:text-red-400">
                      +{r.items_failed} failed
                    </span>
                  )}
                </td>
                <td className="py-1.5 pr-3 text-right text-slate-500 tabular-nums dark:text-slate-400">
                  {money(r.estimate_usd)}
                </td>
                <td className="py-1.5 text-right tabular-nums">
                  {money(r.cost_usd)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {data.recent.length > 5 && (
        <button
          onClick={() => setShowAll(!showAll)}
          className="mt-2 text-sm text-sky-700 hover:underline dark:text-sky-400"
        >
          {showAll ? "Show fewer" : `Show ${data.recent.length - 5} more`}
        </button>
      )}

      {data.unlinked > 0 && (
        <p className="mt-4 text-xs text-slate-500 dark:text-slate-400">
          {data.unlinked} paid results from before spend was recorded aren't
          counted. Run <code>python spend.py --backfill</code> to add them.
        </p>
      )}
    </section>
  );
}

// Header button showing the total; opens and closes the panel.
export function SpendButton({
  total,
  open,
  onClick,
}: {
  total: number;
  open: boolean;
  onClick: () => void;
}) {
  return (
    <button
      onClick={onClick}
      aria-expanded={open}
      title="Money spent on Apify, Claude and Jev"
      className={`rounded-md border px-2 py-1 tabular-nums ${
        open
          ? "border-sky-600 bg-sky-50 text-sky-800 dark:bg-sky-900/40 dark:text-sky-200"
          : "border-slate-300 bg-white hover:border-slate-400 dark:border-slate-600 dark:bg-slate-800"
      }`}
    >
      Spent {money(total)}
    </button>
  );
}

function SourceRow({ data }: { data: SourceSpend }) {
  const facts =
    data.source === "unknown"
      ? "AI runs whose jobs were deleted after another site was added"
      : [
          `${data.jobs} jobs`,
          data.duplicates ? `${data.duplicates} duplicates` : null,
          `${data.kept} kept`,
          data.kept ? `${money(data.cost_usd / data.kept)} per kept job` : null,
        ]
          .filter(Boolean)
          .join(" · ");
  return (
    <li>
      <div className="flex justify-between gap-3">
        <span className="font-medium text-slate-800 dark:text-slate-100">
          {data.label}
        </span>
        <span className="font-medium tabular-nums">{money(data.cost_usd)}</span>
      </div>
      <div className="text-xs text-slate-500 dark:text-slate-400">{facts}</div>
      {data.by_step.length > 0 && (
        <ul className="mt-1 space-y-0.5 pl-3 text-xs text-slate-600 dark:text-slate-300">
          {data.by_step.map((step) => (
            <li key={step.step} className="flex justify-between gap-3">
              <span>{step.label}</span>
              <span className="tabular-nums">{money(step.cost_usd)}</span>
            </li>
          ))}
        </ul>
      )}
    </li>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <div className="text-xs text-slate-500 dark:text-slate-400">{label}</div>
      <div className="text-2xl font-semibold tabular-nums">{value}</div>
    </div>
  );
}
