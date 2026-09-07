"""Validate v2 reproduces v1-style ext column-group gains on stats_CEB_single.

Plan up front (see repo memory): stats_CEB_single tables are SMALL (posts ~92k,
votes ~328k, ...) so L2 (100%) is fast here and gives stable/deterministic reads
for the selective range predicates (L1 shallow sampling was high-variance:
users time-range L0 est=1/qerr~38103, posts st.84 L1 worse than L0).

This probe measures, per selected posts query, the no-ext baseline and a small
set of 2-col MCV candidates over the query's correlated count/score columns at
L2, on each backend, showing the ext q-error reduction (posts baseline ~2.2 is
the "underestimate that ext fixes" pattern from v1).

Usage: cebsi_ext.py <postgres|oracle> [-1 query list]
"""
import sys, re
from extstats2.bench import load_benchmark
from extstats2.config import DBConfig, get_backend
from extstats2.core.candidates import iter_candidates_for_query
from extstats2.core.measure_sampling import measure_query_sampling

be_name = sys.argv[1] if len(sys.argv) > 1 else "postgres"
if be_name == "postgres":
    cfg = DBConfig(host="localhost", port=5432, user="postgres",
                   password="postgres", dbname="stats")
else:
    cfg = DBConfig(host="localhost", port=1521, user="SYSTEM",
                   password="lxf82073077", service="FREEPDB1")
be = get_backend(be_name, cfg=cfg)

Q = {q.qid: q for q in load_benchmark("stats_ceb_single")}
queries = [Q[k] for k in ("st.26", "st.84", "st.23") if k in Q]

# candidate pairs for each table/query: arity-2 over predicate cols
# (posts correlated count/score cols). table key used by backend stats calls.
TBL = {"posts": ".posts", "users": ".users"}

for q in queries:
    m = re.search(r"\bFROM\s+(\w+)", q.sql, re.I)
    tbl = m.group(1).lower() if m else "posts"
    tkey = TBL.get(tbl, "." + tbl)
    # clean any leftover ext on that table first
    for s in list(be.list_stats(tkey)):
        be.drop_stat(s)
    cands = [c for c in iter_candidates_for_query(q, arities=(2,))]
    print(f"[{be_name}] {q.qid} table={tkey} n_arity2_cands={len(cands)} truth={q.ground_truth}")
    # measure at L2 only (small table; stable)
    block = measure_query_sampling(be, q, cands[:40], levels=(2,), outdir=None)
    for lv, L in block["by_lambda"].items():
        base = L["baseline"]["qerror"]
        cs = sorted(L["candidates"], key=lambda c: c["qerror"])
        # best per-colset (lowest across params)
        print(f"   L{lv}: base qerr={base:.3f}  top cols:")
        for c in cs[:5]:
            print(f"      {'.'.join(sorted(c['cols']))} p={c['param']} qerr={c['qerror']:.3f} est={c['estimate']}")
    for s in list(be.list_stats(tkey)):
        be.drop_stat(s)
print(f"[{be_name}] CEBSI EXT PROBE DONE")
