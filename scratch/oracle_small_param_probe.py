"""Empirically answer: would an Oracle param axis [2,4,6,8,10] be better?

Test on q.184's proven driver pair (iDisabl1,iRspouse) at L1 (user convention:
quick experiments use L1/estimate_percent 10, not L2). Sweep a wide SIZE grid
{1,2,3,4,6,8,10,15,25,50,100} and read back *realized* num_buckets + estimate +
qerr. Maps the degrading-cap -> natural-plateau curve: small SIZE is a HARD cap
(SIZE=2 already gave exactly 2 buckets and qerr ~21.7M at L2); we want to see at
L1 where raising SIZE stops buying precision (elbow) and whether [2..10] misses it.
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
q = next(x for x in load_benchmark("census") if x.qid == "query.184")
mcv = next(c for c in be.supported_capabilities() if c.name == "mcv")
TQ = be._q_table(".climate")
COLS = ("iDisabl1", "iRspouse")

def hidden_buckets(cols):
    want = be._q_cols(cols)
    with be._cur() as cur:
        cur.execute("SELECT extension, extension_name FROM user_stat_extensions "
                    "WHERE table_name=:t", {"t": TQ})
        hidden = next((_text(nm) for expr, nm in cur.fetchall()
                       if tuple(_parse_extension_expression(_text(expr))) == want), None)
    if hidden is None:
        return None
    with be._cur() as cur:
        cur.execute("SELECT num_buckets FROM user_tab_col_statistics "
                    "WHERE table_name=:t AND column_name=:c", {"t": TQ, "c": hidden})
        r = cur.fetchone()
    return int(r[0]) if r else None

LV = 1  # user convention: quick Oracle experiments use L1 (~2 s/gather)

def gather(size):
    obj = StatObject(table=".climate", columns=list(COLS), capability=mcv,
                     capacity=Capacity(LV), name=f"small_{size}")
    be.create_stat(obj)
    try:
        be.build_stat_param(obj, size)
        nb = hidden_buckets(COLS)
        est = be.estimate(q)
        return nb, est.estimate, est.qerror
    finally:
        be.drop_stat(obj)

print(f"q.184 truth={q.ground_truth} | driver pair {COLS} @ L{LV}")
be.enter_sampling_state(".climate", LV)
b = be.estimate(q).qerror
print(f"L{LV} no-ext baseline qerr={b:.1f}")
print(f"{'SIZE p':>7}{'realized_buckets':>17}{'est':>10}{'qerr':>8}")
for p in [1, 2, 3, 4, 6, 8, 10, 15, 25, 50, 100]:
    try:
        nb, est, qerr = gather(p)
    except Exception as e:
        print(f"{p:>7}  ERROR {str(e)[:50]}"); break
    print(f"{p:>7}{nb:>17}{est:>10}{qerr:>8.3f}")
for s in list(be.list_stats(".climate")):
    be.drop_stat(s)
print("SMALL-SIZE SWEEP DONE")
