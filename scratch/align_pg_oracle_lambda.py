"""Measure census_mini subset on Oracle and align PG vs Oracle per-lambda output.

Same workload name/query picks/candidate set as the existing PG census_mini run
(scratch/measure_mini_lambda.py) so the two land under:
    results/measure/census_mini/<postgres|oracle>/<qid>.json (+ _meta.json)

Note: the two engines realize an "abstract λ level" differently and the S_rows
per level differ (~20% at L0/L1) because PG sets target -> minrows=300*target
(30k/300k) while Oracle uses estimate_percent*N (24.5k/245k). This alignment is
exactly what the _meta now records (single_target vs estimate_percent), so we
can compare baselines and, per (cols, param), candidate q-error while flagging
Direction-B pruning (param absent on Oracle when > its S/300 cap).
"""
from pathlib import Path

from extstats2.bench import load_benchmark
from extstats2.config import DBConfig, get_backend
from extstats2.core.candidates import generate_candidates_per_query
from extstats2.core.measure_sampling import measure_workload_sampling
from extstats2.core.measure_io import result_dir

# ---- remeasure on Oracle ----
be = get_backend("oracle", cfg=DBConfig(host="localhost", port=1521,
              user="SYSTEM", password="lxf82073077", service="FREEPDB1"))
for s in list(be.list_stats(".climate")):
    be.drop_stat(s)

Q = load_benchmark("census")
picks = ["query.184", "query.62", "query.61"]
qs = [q for q in Q if q.qid in picks]
cands = {}
for q in qs:
    c = generate_candidates_per_query([q], arities=(2,))[q.qid]
    cands[q.qid] = c[:3]
    print("[oracle]", q.qid, "truth=", q.ground_truth, "keep3:", len(cands[q.qid]))

measure_workload_sampling(be, qs, cands, levels=(0, 1, 2),
                        workload="census_mini", outdir=Path("results"))

for s in list(be.list_stats(".climate")):
    be.drop_stat(s)   # restore natural (no column groups)
print("ORACLE MEASURE DONE")

# ---- compare ----
import json
from extstats2.core.measure_io import read_meta

pg = result_dir(Path("results"), "census_mini", "postgres")
or_ = result_dir(Path("results"), "census_mini", "oracle")
def load(qid):
    with open(pg / f"{qid}.json") as f: p = json.load(f)
    with open(or_ / f"{qid}.json") as f: o = json.load(f)
    return p, o

mp = read_meta(pg); mo = read_meta(or_)
def tier_meta(m, lv):
    return next((t for t in m.tiers if t.level == lv), None)

def key(cols, same=True):
    return tuple(str(c).lower() for c in cols)

print("\n=== _meta per level (S / native knob) ===")
for lv in (0, 1, 2):
    tp, to = tier_meta(mp, lv), tier_meta(mo, lv)
    print(f"L{lv}: PG S={tp.S_rows:.0f} single_target={tp.single_target} "
          f"| OR S={to.S_rows:.0f} estimate_percent={to.estimate_percent}")

print("\n=== per query / per lambda: baseline + aligned candidates ===")
for qid in picks:
    p, o = load(qid)
    print(f"\n--- {qid} (actual={p['actual']}) ---")
    for lv in (0, 1, 2):
        bP = p["by_lambda"].get(str(lv)); bO = o["by_lambda"].get(str(lv))
        if not bP or not bO:
            print(f"  L{lv}: missing on one side; skippable"); continue
        print(f"  L{lv}: PG base qerr={bP['baseline']['qerror']:.3f} "
              f"(est {bP['baseline']['estimate']}) | "
              f"OR base qerr={bO['baseline']['qerror']:.3f} "
              f"(est {bO['baseline']['estimate']})")
        # candidate index by (cols-lower, param)
        iP = {(key(c['cols']), c['param']): c for c in bP['candidates']}
        iO = {(key(c['cols']), c['param']): c for c in bO['candidates']}
        for k in sorted(iP):
            cP, cO = iP[k], iO.get(k)
            row = f"    (cols:{list(k[0])}, p={k[1]:<5}) PG={cP['qerror']:.3f}"
            if cO is not None:
                row += f"  OR={cO['qerror']:.3f}"
            elif cO is None and k in iP:
                row += f"  OR ABSENT (Direction-B? > oracle S/300 cap)"
            print(row)
print("\nALIGN DONE")
