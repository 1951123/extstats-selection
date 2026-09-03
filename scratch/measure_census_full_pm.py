"""Full PG census per-lambda Protocol-M measurement (arity-2 candidates).

Runs every census query's arity-2 candidates (per-query, ~9,998 pairs) through
the per-lambda Protocol-M scheduler at L0+L1 (PG fabric; deterministic per-λ),
writing results/per_lambda/census/postgres/ (same namespace the optimizer
consumer reads).

Conventions: levels default L0,L1 (L2 off); param_tiers=None => PG grid
(25,50,100,1000,10000) capped by lattice per level. use_protocol_m => one
ANALYZE per (λ, query) shared across its candidates.
"""
from pathlib import Path

from extstats2.bench import load_benchmark
from extstats2.config import DBConfig, get_backend
from extstats2.core.candidates import generate_candidates_per_query
from extstats2.core.measure_lambda import measure_workload_lambda

be = get_backend("postgres", cfg=DBConfig(host="localhost", port=5432,
               user="postgres", password="postgres", dbname="census"))
for s in list(be.list_stats(".climate")):
    be.drop_stat(s)

Q = load_benchmark("census")
print(f"[run] census queries={len(Q)} arity=2 via Protocol-M")

# per-query arity-2 candidates (un-deduped per query = v1 measure granularity)
cands = {}
for q in Q:
    g = generate_candidates_per_query([q], arities=(2,)).get(q.qid, [])
    cands[q.qid] = g
n = sum(len(v) for v in cands.values())
print(f"[run] total (query,arity2-cand) pairs = {n}")

measure_workload_lambda(be, Q, cands, workload="census",
                        outdir=Path("results"), use_protocol_m=True)

for s in list(be.list_stats(".climate")):
    be.drop_stat(s)
print("[run] DONE -> results/per_lambda/census/postgres/")
