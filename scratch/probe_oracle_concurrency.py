"""Concurrency probe: does N isolated Oracle worker schemas actually parallelize
on Oracle Database Free (cpu_count=2, SGA 1.5G), or do they contend on the
instance?

Creates worker schema CENSL0W2 (copy of CLIMATE) if needed, then measures the
SAME census query (query.61) on CENSL0W1 and CENSL0W2 AT THE SAME TIME in two
processes, and reports each worker's wall-time plus overall wall-time.

If the two finish in ~= 1x single time (10.4s) each, overlapping wall ~10-12s,
parallelism gives a real speedup on this instance.  If they each stretch to ~2x
and overlap to ~21s, the 2-CPU instance serializes them and mirror parallelism
won't help.
"""
from __future__ import annotations

import multiprocessing as mp
import sys
import time
from pathlib import Path

sys.path.insert(0, "src")

import oracledb
from extstats2.config import DBConfig, get_backend
from extstats2.core.measure_sampling import measure_query_sampling
from extstats2.core.candidates import generate_candidates_per_query

WORKERS = {"CENSL0W1": "workerpw1", "CENSL0W2": "workerpw2"}


def ensure_worker(user: str, pw: str) -> None:
    con = oracledb.connect(user="SYSTEM", password="lxf82073077",
                           dsn="localhost:1521/FREEPDB1")
    cur = con.cursor()
    try:
        cur.execute("SELECT COUNT(*) FROM all_tables WHERE owner=:1 AND "
                    "table_name='CLIMATE'", [user])
        has = cur.fetchone()[0]
    except oracledb.DatabaseError:
        has = 0
    if has:
        print(f"[probe] {user}.CLIMATE already present", flush=True)
        con.close()
        return
    try:
        cur.execute(f"DROP USER {user} CASCADE")
    except oracledb.DatabaseError:
        pass
    cur.execute(f"CREATE USER {user} IDENTIFIED BY {pw}")
    cur.execute(f"GRANT CONNECT, RESOURCE, CREATE SESSION TO {user}")
    cur.execute(f"GRANT UNLIMITED TABLESPACE TO {user}")
    cur.execute(f"CREATE TABLE {user}.CLIMATE AS SELECT * FROM SYSTEM.CLIMATE")
    con.commit()
    print(f"[probe] created {user}.CLIMATE", flush=True)
    con.close()


def worker(user: str, pw: str, qid: str, out: str) -> float:
    WW = DBConfig(host="localhost", port=1521, user=user, password=pw,
                  service="FREEPDB1")
    be = get_backend("oracle", cfg=WW)
    for s in list(be.list_stats(".climate")):
        be.drop_stat(s)
    be.restore_natural_stats(".climate", estimate_percent=100.0)
    from extstats2.bench import load_benchmark
    Q = {q.qid: q for q in load_benchmark("census")}
    q = Q[qid]
    cands = generate_candidates_per_query([q], arities=(2,))[qid]
    odir = Path(out) / user
    odir.mkdir(parents=True, exist_ok=True)
    t = time.time()
    measure_query_sampling(be, q, cands, levels=(0,), param_tiers=None,
                         outdir=odir)
    dt = time.time() - t
    print(f"[probe][{user}] measured {qid} (n_cands={len(cands)}) in "
          f"{dt:.1f}s", flush=True)
    return dt


if __name__ == "__main__":
    mp.set_start_method("fork")
    ensure_worker("CENSL0W1", "workerpw1")
    ensure_worker("CENSL0W2", "workerpw2")

    # single-worker baseline reference on W2
    t0 = time.time()
    single = worker("CENSL0W2", "workerpw2", "query.61", "/tmp/probec")
    print(f"[probe] SINGLE baseline on CENSL0W2 = {single:.1f}s", flush=True)

    # concurrent on W1 + W2
    t0 = time.time()
    with mp.Pool(2) as pool:
        r = pool.starmap(worker, [
            ("CENSL0W1", "workerpw1", "query.61", "/tmp/probec"),
            ("CENSL0W2", "workerpw2", "query.61", "/tmp/probec"),
        ])
    wall = time.time() - t0
    print(f"[probe] CONCURRENT 2 workers: each ~{max(r):.1f}s, "
          f"wall={wall:.1f}s (single=~{single:.1f}s) => "
          f"speedup ~{single/wall:.2f}x over serial 2x", flush=True)
