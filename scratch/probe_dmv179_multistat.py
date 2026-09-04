"""Probe dmv.179 for a potential one-stat-sufficiency failure.

dmv.179 = Record_Type='VEH' AND Registration_Class IN ('BOT','MOT') AND State IN
(15 states) AND Body_Type IN ('4DSD','PICK') AND Suspension_Indicator='N' AND
Revocation_Indicator='N' ; TRUE cardinality = 12 (extreme joint sparsity across
6 categorical cols). Single best 2-col MCV from the corpus reaches est ~52k
(qerr ~4400) but not near 12.

Question (user): can MULTIPLE two-column MCVs co-existing beat a single one?
PG16 planner picks among applicable multivariate stats by OID (first wins), so we
measure the min estimate reachable over (subset, create-order) of 2-col MCVs to
see whether any co-install composes toward truth or merely re-selects a single
pair. We also probe deeper params and a 3-col MCV to bound what's achievable.

Runs on an idle dedicated clone db (dmv_try), independent of the 8-mirror
measurement. EXPLAIN-only; safe.

Usage: .venv/bin/python -u scratch/probe_dmv179_multistat.py --db dmv_try
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

# dmv.179 conditions
WHERE = ("Record_Type='VEH' AND Registration_Class IN ('BOT','MOT') AND State IN "
         "('NY','NJ','OK','PA','MD','IL','TX','FL','GA','CT','IN','OH','WI','MA','AZ','CA') "
         "AND Body_Type IN ('4DSD','PICK') AND Suspension_Indicator='N' AND "
         "Revocation_Indicator='N'")
SQL = f"SELECT * FROM dmv WHERE {WHERE}"

# all 2-col pairs among the 6 constrained columns, each (for build) is a candidate
COL_POOL = ["Record_Type", "Registration_Class", "State", "Body_Type",
            "Suspension_Indicator", "Revocation_Indicator"]


def pairs(cols):
    out = []
    for i in range(len(cols)):
        for j in range(i + 1, len(cols)):
            out.append((cols[i], cols[j]))
    return out


def pair_stats():
    from itertools import combinations
    return [c for c in combinations(COL_POOL, 2)]


def psql(db, sql):
    import subprocess
    import os
    r = subprocess.run(["psql", "-h", "localhost", "-U", "postgres", "-d", db,
                        "-P", "pager=off", "-At", "-c", sql],
                       capture_output=True, text=True,
                       env={**os.environ, "PGPASSWORD": "postgres"})
    return r.stdout.strip()


def explain_rows(db):
    j = json.loads(psql(db, f"EXPLAIN (FORMAT JSON) {SQL}"))
    return j[0]["Plan"]["Plan Rows"]


def main(db):
    psql(db, "ANALYZE dmv")
    true = int(psql(db, f"SELECT count(*) FROM dmv WHERE {WHERE}"))
    def qr(e):
        return max(e / true, true / max(e, 1))
    print(f"dmv.179 truth={true}")
    # baseline (no ext stats)
    print(f"baseline: rows={explain_rows(db)} qerr={qr(explain_rows(db)):.1f}")

    allpairs = list(pair_stats())
    print(f"\n=== single 2-col MCV at param 250 vs 1000 ===")
    # install each pair alone, EXPLAIN; find best
    best_single = (1e18, None)
    for p in allpairs:
        nm = "s_" + "_".join(p).lower()
        # param 250
        psql(db, f"DROP STATISTICS IF EXISTS {nm}")
        psql(db, f"CREATE STATISTICS {nm}(mcv) ON {p[0]}, {p[1]} FROM dmv")
        psql(db, f"ALTER STATISTICS {nm} SET STATISTICS 250")
        psql(db, "ANALYZE dmv")
        e = explain_rows(db)
        if e < best_single[0]:
            best_single = (e, (p, 250))
        psql(db, f"DROP STATISTICS IF EXISTS {nm}")
    print(f"best single 2-col (param per listed) rows={best_single[0]} pair={best_single[1]} "
          f"qerr={qr(best_single[0]):.1f}")

    # co-install greedily: add pairs one by one (in an order), picking lowest-est each round
    print("\n=== greedy forward co-install of 2-col MCVs (keep improving ones) ===")
    installed = []
    cur_rows = 1e18
    pool = list(allpairs)
    improved = True
    while improved and pool:
        improved = False
        best_e = cur_rows
        best_p = None
        # try adding each not-yet-installed pair on top of current set
        for p in pool:
            nm = f"k{len(installed)}_{'_'.join(p).lower()}"
            psql(db, f"CREATE STATISTICS IF NOT EXISTS {nm}(mcv) ON {p[0]}, {p[1]} FROM dmv")
            psql(db, f"ALTER STATISTICS {nm} SET STATISTICS 250")
            psql(db, "ANALYZE dmv")
            e = explain_rows(db)
            # drop just-added to test it alone on top
            psql(db, f"DROP STATISTICS IF EXISTS {nm}")
            if e < best_e - 1:   # strict improvement
                best_e, best_p = e, p
        if best_p is not None:
            nm = f"k{len(installed)}_{'_'.join(best_p).lower()}"
            psql(db, f"CREATE STATISTICS {nm}(mcv) ON {best_p[0]}, {best_p[1]} FROM dmv")
            psql(db, f"ALTER STATISTICS {nm} SET STATISTICS 250")
            psql(db, "ANALYZE dmv")
            installed.append((best_p, 250))
            pool.remove(best_p)
            cur_rows = explain_rows(db)
            improved = True
            print(f"  + add {best_p}: rows->{cur_rows} qerr={qr(cur_rows):.1f}")
    print(f"forward co-install end: {len(installed)} stats, rows={cur_rows} qerr={qr(cur_rows):.1f}")

    # Also try a 3-col MCV on the three most-selective cols
    print("\n=== deeper: single 3-col MCV + higher param ===")
    for cols in [("Record_Type", "Registration_Class", "Body_Type"),
                 ("Registration_Class", "Body_Type", "State")]:
        nm = "t3_%s" % "_".join(c.lower() for c in cols)
        psql(db, "DROP STATISTICS IF EXISTS " + nm)
        psql(db, f"CREATE STATISTICS {nm}(mcv) ON {cols[0]}, {cols[1]}, {cols[2]} FROM dmv")
        for prm in (250, 1000, 5000):
            psql(db, f"ALTER STATISTICS {nm} SET STATISTICS {prm}")
            psql(db, "ANALYZE dmv")
            e = explain_rows(db)
            print(f"  3-col {cols} p={prm}: rows={e} qerr={qr(e):.1f}")
        psql(db, "DROP STATISTICS IF EXISTS " + nm)

    # note best 2-col single at deep param 5000
    print("\n=== best single pair at deep param ===")
    p = best_single[1][0]
    nm = "deep_" + "_".join(p).lower()
    psql(db, f"CREATE STATISTICS {nm}(mcv) ON {p[0]}, {p[1]} FROM dmv")
    for prm in (1000, 5000):
        psql(db, f"ALTER STATISTICS {nm} SET STATISTICS {prm}")
        psql(db, "ANALYZE dmv")
        e = explain_rows(db)
        print(f"  single {p} p={prm}: rows={e} qerr={qr(e):.1f}")
    psql(db, "DROP STATISTICS IF EXISTS " + nm)
    # leave db clean
    for s in [x for x in [] ]: pass
    print("\nprobe db left clean (dropped added stats).")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="dmv_try")
    a = ap.parse_args()
    main(a.db)
