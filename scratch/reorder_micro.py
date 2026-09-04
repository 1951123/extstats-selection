"""Micro experiment: does CREATE (OID) order of overlapping column-MCV statistics
change a query's TRUE deployed estimate on PG16?

Tests query.274 (extreme-sparse; mask-best single stat (dRearning,dWeek89)@p25
gives qerr~1.08, baseline ~108). On a fresh mirror DB we deploy small controlled
sets and EXPLAIN query.274 under each OID (CREATE) order:
  A: best stat        (dRearning, dWeek89) @ p25
  B: poorer           (dRearning, iWork89) @ p10   (overlaps on dRearning)
  C: another poorer   (dWeek89, iMeans)    @ p5
Learns whether order/co-presence of column-overlapping MCVs changes the TRUE est.
"""
from __future__ import annotations
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from extstats2.config import DBConfig, get_backend
from extstats2.backend.base import StatObject
from extstats2.backend.capabilities import Capacity
from extstats2.bench import load_benchmark

QUERY_QID = "query.274"


def backend_for(db):
    return get_backend("postgres", cfg=DBConfig(
        host="localhost", port=5432, user="postgres", password="postgres", dbname=db))


def deploy(db, objs_params):
    """Deploy stats in given creation order (ascending OID), ANALYZE once."""
    be = backend_for(db)
    cap = [c for c in be.supported_capabilities() if c.name == "mcv"][0]
    be.enter_lambda_state(".climate", 1)
    for s in list(be.list_stats(".climate")):
        be.drop_stat(s)
    objs = []
    for cols, p in objs_params:
        cols_t = "_".join(c.lower() for c in sorted(cols))
        obj = StatObject(table=".climate", columns=tuple(sorted(cols)),
                         capability=cap, capacity=Capacity(p, "mcv"),
                         name=f"od_{cols_t}_p{p}")
        be.create_stat(obj)                 # OID assigned here, ascending order
        objs.append((obj, p))
    for obj, p in objs:                     # per-object retained target
        with be.conn.cursor() as cur:
            cur.execute(f"ALTER STATISTICS {obj.name} SET STATISTICS {int(p)}")
    with be.conn.cursor() as cur:           # single shared ANALYZE
        cur.execute("ANALYZE climate")
    return be


def run(db):
    q = [x for x in load_benchmark("census") if x.qid == QUERY_QID][0]
    truth = q.ground_truth
    A = (("dRearning", "dWeek89"), 25)
    B = (("dRearning", "iWork89"), 10)
    C = (("dWeek89", "iMeans"), 5)
    for label, s in [
        ("baseline (no ext)", []),
        ("A only", [A]),
        ("A,B (A first)", [A, B]),
        ("B,A (B first)", [B, A]),
        ("A,B,C (A first)", [A, B, C]),
        ("C,B,A (C first)", [C, B, A]),
        ("B,C,A (B,C first)", [B, C, A]),
    ]:
        be = deploy(db, s)
        e = be.estimate(q)
        est = e.estimate
        qerr = (max(est / truth, truth / est) if est and truth else float("nan"))
        print(f"{label:28s}: est={est:>10,}   qerr={qerr:8.2f}   (truth={truth:,})")


if __name__ == "__main__":
    run(sys.argv[1] if len(sys.argv) > 1 else "census_e2e2")
