"""Prototype: validate per-worker Oracle user schema (mirror-table) parallelism.

Creates ONE worker schema ``CENSL0W1`` (Oracle user) with a copy of SYSTEM.CLIMATE
(and its own plan_table via EXPLAIN PLAN on demand), then measures one census
query at L0 against that copy by connecting AS the worker user — the exact model
the N-way parallel mirror driver will use (mirrors PG's ``census_m1..mN``).

If this runs cleanly and ~fast, the full parallel driver is built on it.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, "src")

import oracledb
from extstats2.config import DBConfig, get_backend
from extstats2.core.measure_lambda import measure_query_lambda
from extstats2.core.candidates import generate_candidates_per_query

SYSTEM = dict(user="SYSTEM", password="lxf82073077",
              dsn="localhost:1521/FREEPDB1")
WORKER = "CENSL0W1"
WPW = "workerpw1"


def admin():
    return oracledb.connect(**SYSTEM)


def ensure_worker() -> None:
    con = admin()
    cur = con.cursor()
    # drop if present (idempotent for re-runs)
    try:
        cur.execute(f"DROP USER {WORKER} CASCADE")
    except oracledb.DatabaseError:
        pass
    cur.execute(f"CREATE USER {WORKER} IDENTIFIED BY {WPW}")
    cur.execute(f"GRANT CONNECT, RESOURCE, CREATE SESSION TO {WORKER}")
    cur.execute(f"GRANT UNLIMITED TABLESPACE TO {WORKER}")
    # copy CLIMATE (+ its columns/data) into the worker schema
    cur.execute(f"CREATE TABLE {WORKER}.CLIMATE AS SELECT * FROM SYSTEM.CLIMATE")
    con.commit()
    print(f"[proto] created worker schema {WORKER} + CLIMATE copy", flush=True)
    con.close()


def worker_backend_measure() -> None:
    WW = DBConfig(host="localhost", port=1521, user=WORKER, password=WPW,
                  service="FREEPDB1")
    be = get_backend("oracle", cfg=WW)
    for s in list(be.list_stats(".climate")):
        be.drop_stat(s)
    be.restore_natural_stats(".climate", estimate_percent=100.0)
    from extstats2.bench import load_benchmark
    Q = {q.qid: q for q in load_benchmark("census")}
    q = Q["query.61"]
    cands = generate_candidates_per_query([q], arities=(2,))["query.61"]
    t = time.time()
    measure_query_lambda(be, q, cands, levels=(0,), param_tiers=None,
                         outdir=Path("/tmp/protow"))
    print(f"[proto] measured query.61 on {WORKER}.CLIMATE in "
          f"{time.time()-t:.1f}s (n_cands={len(cands)})", flush=True)


if __name__ == "__main__":
    ensure_worker()
    worker_backend_measure()
    print("[proto] OK", flush=True)
