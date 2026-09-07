"""Physically deploy a Phase-2 order JSON on a fresh mirror and EXPLAIN 468.

Reads results/e2e_order_L1_100KB.json (from e2e_order_deploy.py) containing the
CREATE order (list of stat_keys "|col_a,col_b|P<param>"), builds that exact
ordered set on a mirror DB clone, EXPLAINs all census queries, and compares the
TRUE deployed mean/geomean/max vs the order-aware model realised_mean vs the
interference-free predicted_mean.

Usage:
  create census_e2e3 first, then:
  .venv/bin/python -u scratch/deploy_phase2_order.py --order results/e2e_order_L1_100KB.json \
      --db census_e2e3 --out results/e2e_true_ordered_L1_100KB.json
"""
from __future__ import annotations
import argparse, json, re, sys, time
from pathlib import Path
import numpy as np
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

def mk_statkey(cols, param):
    return f"|{','.join(sorted(cols))}|P{param}"

def parse_statkey(key):
    # "|a,b|P100"
    m = re.match(r"\|(.*)\|P(\d+)", key)
    cols = tuple(sorted(x.strip().lower() for x in m.group(1).split(",")))
    return cols, int(m.group(2))

def metrics(qs):
    q = np.asarray([max(float(v),1.0) for v in qs], float)
    return {"mean":float(q.mean()),"geo":float(np.exp(np.log(q).mean())),"max":float(q.max())}

def main(ord_json, db, table, level, out):
    from extstats2.config import DBConfig, get_backend
    from extstats2.backend.base import StatObject
    from extstats2.backend.capabilities import Capacity
    from extstats2.bench import load_benchmark

    data=json.load(open(ord_json))
    order=data["order"]
    rmodel = data.get("realised_mean") if data.get("realised_mean") is not None else data.get("realised_model_mean")
    pred = data.get("predicted_mean")
    print(f"order len {len(order)}  realised_model={rmodel if rmodel is not None else float('nan'):.4f} pred={pred if pred is not None else float('nan'):.4f}")
    be=get_backend("postgres", cfg=DBConfig(host="localhost",port=5432,user="postgres",password="postgres",dbname=db))
    cap=[c for c in be.supported_capabilities() if c.name=="mcv"][0]
    be.enter_sampling_state(table, int(level))
    for s in list(be.list_stats(table)):
        be.drop_stat(s)
    # create stats in the given ORDER (OID ascending = first is lowest OID)
    created=[]
    for i,key in enumerate(order):
        cols,param=parse_statkey(key)
        nm=f"ph2_{i}_{'_'.join(cols)}_p{param}"
        obj=StatObject(table=table, columns=cols, capability=cap,
                      capacity=Capacity(param,"mcv"), name=nm)
        be.create_stat(obj)
        created.append((obj,param))
    # set per-stat target then one shared ANALYZE
    for obj,param in created:
        with be.conn.cursor() as cur:
            cur.execute(f"ALTER STATISTICS {obj.name} SET STATISTICS {int(param)}")
    with be.conn.cursor() as cur:
        cur.execute(f"ANALYZE {table.lstrip('.')}")
    print(f"deployed {len(created)} in phase-2 order; shared ANALYZE done")

    # EXPLAIN all census queries in deployed state
    queries=load_benchmark("census")
    def num(q): return int(q.qid.split('.')[1])
    queries_sorted=sorted(queries,key=num)
    rows=[]
    for q in queries_sorted:
        e=be.estimate(q)
        est=float(e.estimate) if e.estimate else float("nan")
        truth=float(q.ground_truth) if q.ground_truth else float("nan")
        qe=max(est/truth,truth/est) if est==est and truth==truth else float("nan")
        rows.append({"qid":q.qid,"truth":truth,"est":est,"qerr":qe})
    qerrs=[r["qerr"] for r in rows if r["qerr"]==r["qerr"]]
    m=metrics(qerrs)
    result={"db":db,"level":level,
            "predicted_mean":data.get('predicted_mean'),
            "realised_model_mean":data.get('realised_mean'),
            "true_metrics":m, "n":len(qerrs),
            "per_query":{r["qid"]:r["qerr"] for r in rows}}
    json.dump(result, open(out,"w"), indent=1)
    print("TRUE deployed (ordered):",m)
    print("vs predicted=",data.get('predicted_mean')," model-realised=",data.get('realised_mean'))
    print(f"wrote {out}")

if __name__=="__main__":
    ap=argparse.ArgumentParser()
    ap.add_argument("--order",default=str(ROOT/"results"/"e2e_order_L1_100KB.json"))
    ap.add_argument("--db",default="census_e2e3")
    ap.add_argument("--table",default=".climate")
    ap.add_argument("--level",type=int,default=1)
    ap.add_argument("--out",default=str(ROOT/"results"/"e2e_true_ordered_L1_100KB.json"))
    a=ap.parse_args()
    main(a.order,a.db,a.table,a.level,Path(a.out))
