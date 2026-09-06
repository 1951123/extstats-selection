"""Reproducible PG S-grid 3-benchmark analysis over the measured corpora.

Reports, for each bench, per-lambda (L0/L1): baseline q-error distribution,
per-query best-candidate q-error (single 2-col stat, interference-free upper
bound), improveable count, heavy-tail / 2-col-unrepairable counts, and physical
(colset,param) scope — all over the **candidate-bearing query set** denominator
(see docs/reporting-convention.md).

Corpus : results/measure/<bench>/postgres/  (census=query.*, stats_ceb_single
         =st.*, dmv=dmv.*) — S-grid levels {0,1}.
Output : results/report_pg_sgrid_3bench.json  (writes it) + console table.

Usage::
    python scratch/analyze_pg_sgrid.py
    python scratch/analyze_pg_sgrid.py --out results/report_pg_sgrid_3bench.json
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# file-name prefixes per bench
PREFIX = {"census": "query", "stats_ceb_single": "st", "dmv": "dmv"}
# total benchmark query counts (for N/M transparency)
TOTAL = {"census": 468, "stats_ceb_single": 632, "dmv": 1965}
BENCHES = ["census", "stats_ceb_single", "dmv"]


def _vals(a):
    out = []
    for x in a:
        try:
            f = float(x)
            if math.isnan(f) or math.isinf(f):
                continue
            out.append(f)
        except (TypeError, ValueError):
            continue
    return out


def _summ(a):
    """summary dict over a list of numbers (NaN/inf filtered)."""
    a = sorted(_vals(a))
    if not a:
        return None
    def p(v):
        return a[min(len(a) - 1, int(len(a) * v))]
    return {"n": len(a), "mean": round(statistics.mean(a), 3),
            "geo": round(math.exp(statistics.mean([math.log(x) for x in a])), 3),
            "med": round(p(0.5), 3), "p90": round(p(0.9), 3),
            "min": round(a[0], 3), "max": round(a[-1], 3),
            "n_gt2": int(sum(1 for x in a if x > 2)),
            "n_gt4": int(sum(1 for x in a if x > 4))}


def analyze_bench(bench, d):
    files = sorted(d.glob(f"{PREFIX[bench]}.*.json"))
    res = {lv: {"base": [], "best": []} for lv in ("0", "1")}
    meta = {"ok_files": len(files), "total_queries": TOTAL[bench],
            "truth0_excluded": 0, "malformed": 0, "both_levels": 0}
    improve = {"0": 0, "1": 0}
    colset = {"0": set(), "1": set()}
    phys = {"0": set(), "1": set()}
    for fp in files:
        try:
            b = json.load(open(fp))
        except Exception:
            meta["malformed"] += 1
            continue
        if b.get("actual", 1) == 0:
            meta["truth0_excluded"] += 1
        bl = b.get("by_lambda", {})
        if "0" in bl and "1" in bl:
            meta["both_levels"] += 1
        for lv in ("0", "1"):
            sl = bl.get(lv)
            if not sl:
                continue
            base = sl.get("baseline", {}).get("qerror")
            cands = sl.get("candidates", [])
            qs = [c["qerror"] for c in cands]
            res[lv]["base"].append(base)
            best = min(qs) if qs else None
            res[lv]["best"].append(best)
            if qs and min(qs) < base:
                improve[lv] += 1
            for c in cands:
                colset[lv].add(tuple(c["cols"]))
                phys[lv].add((tuple(c["cols"]), c["param"]))

    out = {"meta": meta}
    for lv in ("0", "1"):
        l = {}
        l["baseline"] = _summ(res[lv]["base"])
        l["best_candidate"] = _summ(res[lv]["best"])
        l["n_improveable"] = improve[lv]
        unrep = sum(1 for bs, bst in zip(res[lv]["base"], res[lv]["best"])
                    if bs > 4 and (bst is not None and bst > 4))
        heavy = sum(1 for bs in res[lv]["base"] if bs > 4)
        l["n_heavy_base_gt4"] = heavy
        l["n_unrepairable_2col_gt4"] = unrep
        l["n_distinct_colset"] = len(colset[lv])
        l["n_phys_colset_param"] = len(phys[lv])
        out[lv] = l
    return out


def fmt(v):
    if not v:
        return "n/a"
    return (f"n={v['n']} mean={v['mean']} geo={v['geo']} med={v['med']} "
            f"p90={v['p90']} max={v['max']} n>2={v['n_gt2']} n>4={v['n_gt4']}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "results" / "report_pg_sgrid_3bench.json"))
    args = ap.parse_args()

    report = {"method": "PG S-grid (levels 0,1) over candidate-bearing set "
                        "(see docs/reporting-convention.md)",
              "benches": {}}
    for bench in BENCHES:
        d = ROOT / "results" / "measure" / bench / "postgres"
        rep = analyze_bench(bench, d)
        report["benches"][bench] = rep
        print("=" * 74)
        print(f"### {bench}/postgres  ok={rep['meta']['ok_files']} "
              f"truth0_exc={rep['meta']['truth0_excluded']} "
              f"both_levels={rep['meta']['both_levels']}")
        for lv in ("0", "1"):
            r = rep[lv]
            print(f"  [L{lv}] baseline        : {fmt(r['baseline'])}")
            print(f"  [L{lv}] best-candidate  : {fmt(r['best_candidate'])}")
            print(f"  [L{lv}] improveable={r['n_improveable']} "
                  f"heavy(base>4)={r['n_heavy_base_gt4']} "
                  f"unrep-by-2col(best仍>4)={r['n_unrepairable_2col_gt4']}")
            print(f"  [L{lv}] distinct colset={r['n_distinct_colset']} "
                  f"phys(colset,param)={r['n_phys_colset_param']}")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=1))
    print(f"\nwrote {out}")


if __name__ == "__main__":
    main()
