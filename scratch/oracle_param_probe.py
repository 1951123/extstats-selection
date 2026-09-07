"""Probe whether the PG-inherited param_tiers (25..10000) fit Oracle's bucket model.

Oracle's DBMS_STATS `SIZE p` on a column group is an upper cap on histogram
buckets, but the number actually realized is bounded by the engine (row count,
distinct-value caps, histogram-type bucket ceilings). If raising `param` no
longer raises the realized `num_buckets` / improves the estimate, that tail of
the PG param grid is "wasted" on Oracle => the grid is not well-suited.

Method: on CLIMATE, at a fixed deep λ (sample depth 100%), build one
column group (DINCOME3, DTRAVTIME) at SIZE p for p in [25, 50, 100, 1000, 10000];
after
each, read back the *realized* num_buckets (USER_TAB_COL_STATISTICS on the hidden
extension col) and the query.62 cardinality estimate; drop. Table shows where it
saturates.
"""
from extstats2.bench import load_benchmark
from extstats2.config import DBConfig, get_backend
from extstats2.backend.base import StatObject
from extstats2.backend.capabilities import Capacity
from extstats2.backend.oracle import _text, _parse_extension_expression

OR = DBConfig(host="localhost", port=1521, user="SYSTEM",
              password="lxf82073077", service="FREEPDB1")
be = get_backend("oracle", cfg=OR)
for s in list(be.list_stats(".climate")):
    be.drop_stat(s)

Q = load_benchmark("census")
q = next((x for x in Q if x.qid == "query.62"), Q[0])
cols = ("dIncome3", "dTravtime")
params = [25, 50, 100, 1000, 10000]
mcv = next((c for c in be.supported_capabilities() if c.name == "mcv"))

TQ = be._q_table(".climate")

def hidden_buckets(cols):
    want = be._q_cols(cols)
    hidden = None
    with be._cur() as cur:
        cur.execute(
            "SELECT extension, extension_name FROM user_stat_extensions "
            "WHERE table_name=:t", {"t": TQ})
        for expr, nm in cur.fetchall():
            if tuple(_parse_extension_expression(_text(expr))) == want:
                hidden = _text(nm)
                break
    if hidden is None:
        return None
    with be._cur() as cur:
        cur.execute(
            "SELECT num_buckets FROM user_tab_col_statistics "
            "WHERE table_name=:t AND column_name=:c", {"t": TQ, "c": hidden})
        r = cur.fetchone()
    return int(r[0]) if r else None

print(f"query {q.qid} truth={q.ground_truth} pair={cols}")
print(f"{'SIZE p':>7} {'realized_buckets':>17} {'estimate':>10} {'qerr':>8}")
# fixed deep lambda state (L2)
be.enter_sampling_state(".climate", 2)
for p in params:
    obj = StatObject(table=".climate", columns=list(cols), capability=mcv,
                     capacity=Capacity(2), name=f"probe_l2_{p}")
    be.create_stat(obj)
    try:
        be.build_stat_param(obj, p)
        nb = hidden_buckets(cols)
        est = be.estimate(q)
        qe = est.qerror if est.qerror is not None else float("nan")
        print(f"{p:>7} {str(nb):>17} {est.estimate:>10} {qe:>8.3f}")
    finally:
        be.drop_stat(obj)
for s in list(be.list_stats(".climate")):
    be.drop_stat(s)
print("PROBE DONE")
