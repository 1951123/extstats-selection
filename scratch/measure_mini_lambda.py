"""Measure a small batch of high-error PG census queries into per-lambda files."""
from pathlib import Path

from extstats2.bench import load_benchmark
from extstats2.config import DBConfig, get_backend
from extstats2.core.candidates import generate_candidates_per_query
from extstats2.core.measure_sampling import measure_workload_sampling

be = get_backend("postgres", cfg=DBConfig(host="localhost", port=5432,
              user="postgres", password="postgres", dbname="census"))
for s in list(be.list_stats(".climate")):
    be.drop_stat(s)

Q = load_benchmark("census")
picks = ["query.184", "query.62", "query.61"]
qs = [q for q in Q if q.qid in picks]
cands = {}
for q in qs:
    c = generate_candidates_per_query([q], arities=(2,))[q.qid]
    cands[q.qid] = c[:3]  # cap for speed
    print(q.qid, "truth=", q.ground_truth, "arity2 cands (keep 3):", len(c))

measure_workload_sampling(be, qs, cands, levels=(0, 1, 2),
                        workload="census_mini", outdir=Path("results"))

for s in list(be.list_stats(".climate")):
    be.drop_stat(s)
be._set_all_columns_target(".climate", 100, analyze=True)

base = Path("results/measure/census_mini")
files = sorted(p.name for p in base.glob("*.json")) if base.exists() else []
print("files written under", base, ":", files)
