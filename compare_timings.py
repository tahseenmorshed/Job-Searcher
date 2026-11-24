"""Compare two runs' timing records (data/timings/*.json, written by pipeline.py): how long each
took, and whether each job ended up in the same category.

Jobs are matched by source and the site's own id, so the two runs can be in different databases
(for example, the same jobs-only snapshot processed by the old code and by the new). Haiku doesn't
always give the same answer twice, so expect a few borderline jobs to change category even with
no code change.

Usage:
  python compare_timings.py                 # the two latest records
  python compare_timings.py OLD.json NEW.json
"""

import json
import sys
from pathlib import Path

TIMINGS_DIR = Path("data/timings")


def load(path):
    record = json.loads(Path(path).read_text())
    record["by_job"] = {(j["source"], j["source_job_id"]): j for j in record["jobs"]}
    return record


def main():
    paths = sys.argv[1:3] or [str(p) for p in sorted(TIMINGS_DIR.glob("run-*.json"))[-2:]]
    if len(paths) != 2:
        sys.exit("Need two timing records: python compare_timings.py OLD.json NEW.json")
    old, new = load(paths[0]), load(paths[1])
    print(f"Old: {paths[0]}  (started {old['started_at']})")
    print(f"New: {paths[1]}  (started {new['started_at']})\n")

    t_old, t_new = old["total_excluding_approval_s"], new["total_excluding_approval_s"]
    print(f"{'Time (excluding approval)':<34} {t_old:>9.1f}s {t_new:>9.1f}s   {t_old / t_new:.1f}x" if t_new else "")
    if old["parts_s"].get("fetch") or new["parts_s"].get("fetch"):
        f_old, f_new = old["parts_s"].get("fetch", 0), new["parts_s"].get("fetch", 0)
        print(f"{'  of which fetch':<34} {f_old:>9.1f}s {f_new:>9.1f}s")
        print(f"{'  without fetch':<34} {t_old - f_old:>9.1f}s {t_new - f_new:>9.1f}s   "
              f"{(t_old - f_old) / (t_new - f_new):.1f}x" if t_new - f_new else "")
    for label, record in (("old", old), ("new", new)):
        print(f"  {label} parts: " + ", ".join(f"{k} {v:.1f}s" for k, v in record["parts_s"].items()))

    print(f"\n{'Counts':<34} {'old':>10} {'new':>10}")
    for key in ("jobs", "listed", "rated"):
        print(f"  {key:<32} {old['counts'][key]:>10} {new['counts'][key]:>10}")
    categories = sorted(set(old["counts"]["by_category"]) | set(new["counts"]["by_category"]))
    for c in categories:
        a, b = old["counts"]["by_category"].get(c, 0), new["counts"]["by_category"].get(c, 0)
        print(f"  {c:<32} {a:>10} {b:>10}{'' if a == b else '   *'}")

    only_old = old["by_job"].keys() - new["by_job"].keys()
    only_new = new["by_job"].keys() - old["by_job"].keys()
    both = old["by_job"].keys() & new["by_job"].keys()
    changed = sorted((k for k in both if old["by_job"][k]["category"] != new["by_job"][k]["category"]),
                     key=lambda k: (old["by_job"][k]["category"], k))
    print(f"\nJobs in both runs: {len(both)} · only in old: {len(only_old)} · only in new: {len(only_new)}")
    print(f"Jobs in both whose category changed: {len(changed)}")
    for k in changed:
        o, n = old["by_job"][k], new["by_job"][k]
        print(f"  {o['category']:>20} -> {n['category']:<20} {o['title']} | {o['company']}  ({k[0]} {k[1]})")
    core_old = {k for k in both if old["by_job"][k]["category"] == "core"}
    core_new = {k for k in both if new["by_job"][k]["category"] == "core"}
    print(f"\nCore in both: {len(core_old & core_new)} · core only in old: {len(core_old - core_new)} · "
          f"core only in new: {len(core_new - core_old)}")
    for label, keys, rec in (("only in old", only_old, old), ("only in new", only_new, new)):
        if keys:
            print(f"\nJobs {label} (different fetches return different jobs):")
            for k in sorted(keys)[:20]:
                print(f"  {rec['by_job'][k]['category']:<20} {rec['by_job'][k]['title']} ({k[0]} {k[1]})")
            if len(keys) > 20:
                print(f"  ... and {len(keys) - 20} more")


if __name__ == "__main__":
    main()
