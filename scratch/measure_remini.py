"""Fresh per-lambda re-measurement under current conventions.

Run-level conventions baked in by the driver defaults (no explicit levels passed
=> DEFAULT_LAMBDA_LEVELS=(0,1), so L2/full-scan is dropped; param_tiers=None =>
each backend's own representation_param_tiers grid).

Candidates per query are drawn from the query's ACTUAL predicates (predicate-
derived) plus any empirically-confirmed driver pair, so the per-lambda data shows
real (driver) q-error movement rather than decoy-flat readings.

Usage: measure_remini.py <backend: postgres|oracle>
Writes results/measure/census_mini/<backend>/.
"""
import sys
from pathlib import Path

from extstats2.bench import load_benchmark
from extstats2.config import DBConfig, get_backend
from extstats2.core.candidates import generate_candidates_per_query
from extstats2.core.measure_lambda import measure_workload_lambda

backend_name = sys.argv[1] if len(sys.argv) > 1 else "postgres"
if backend_name == "postgres":
    cfg = DBConfig(host="localhost", port=5432, user="postgres",
                   password="postgres", dbname="census")
else:
    cfg = DBConfig(host="localhost", port=1521, user="SYSTEM",
                   password="lxf82073077", service="FREEPDB1")
be = get_backend(backend_name, cfg=cfg)
for s in list(be.list_stats(".climate")):
    be.drop_stat(s)

Q = load_benchmark("census")
picks = ["query.184", "query.62", "query.61"]
qs = [q for q in Q if q.qid in picks]

# Small focus set per query (few pairs keeps the fast re-run bounded yet shows the
# driver effect). Columns come FROM the generator so identifier casing is exact.
_FOCUS = {
    # confirmed driver first (see oracle_driver_probe / repo memory),
    # then a couple of alternates/decoy-ish pairs.
    "query.184": [("iDisabl1", "iRspouse"), ("dDepart", "dRpincome"),
                  ("dTravtime", "dRpincome"), ("iRelat2", "dRpincome"),
                  ("iDisabl1", "iImmigr")],
    "query.62":  [("iRspouse", "iWork89"), ("dIncome3", "dTravtime"),
                  ("dIncome3", "iRelat2"), ("iClass", "iRelat2")],
    "query.61":  [("iDisabl2", "iYearsch"), ("dHours", "iLooking"),
                  ("iSept80", "iSubfam2"), ("iTmpabsnt", "iWWII")],
}

cands = {}
for q in qs:
    allp = generate_candidates_per_query([q], arities=(2,)).get(q.qid, [])
    by_key = {frozenset(sorted(c.columns)): c for c in allp}
    sel = []
    for want in _FOCUS[q.qid]:
        c = by_key.get(frozenset(sorted(want)))
        if c is not None and c not in sel:
            sel.append(c)
    cands[q.qid] = sel
    print(f"[{backend_name}] {q.qid} focus candidates: {[sorted(x.columns) for x in sel]}")

print(f"[{backend_name}] measuring {len(qs)} queries, levels=(0,1) default, "
      f"per-backend param grid")
measure_workload_lambda(be, qs, cands, workload="census_mini",
                        outdir=Path("results"))

for s in list(be.list_stats(".climate")):
    be.drop_stat(s)
if backend_name == "postgres":
    be._set_all_columns_target(".climate", 100, analyze=True)
print(f"[{backend_name}] DONE -> results/measure/census_mini/{backend_name}/")
