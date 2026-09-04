"""Stronger Oracle multi-group composition probe (6 conjuncts, two decomps).

Confirms Oracle composes MULTIPLE disjoint column groups for one conjunctive
selection WHERE, beyond the earlier 2-pair case:
  - Scenario I : 6 cols a..f = THREE independent correlated 2-col clusters
                 (a,b),(c,d),(e,f). Query WHERE a=b=c=d=e=f=1.
                 correct deploy = 3 disjoint 2-col groups (a,b)(c,d)(e,f).
  - Scenario II: 6 cols = TWO independent correlated 3-col clusters
                 (a,b,c),(d,e,f). Query same 6 conjuncts.
                 correct deploy = 2 disjoint 3-col groups (a,b,c)(d,e,f).

For each scenario compare EXPLAIN E-Rows under:
  none / decomposition-A (three 2-col) / decomposition-B (two 3-col) / full 6 /
against TRUE 4-way(6-way) count and the independent-composed expectation.

Controlled synthetic tables (deterministic; created+dropped in SYSTEM).

Usage: python scratch/probe_oracle_composition_6way.py
"""
from __future__ import annotations

import json
import re
import sys
import time

import numpy as np
import oracledb

sys.path.insert(0, "src")
OR = dict(user="SYSTEM", password="lxf82073077", dsn="localhost:1521/FREEPDB1")


def conn():
    return oracledb.connect(**OR)


def explain_rows(cur, sql):
    sid = "G6" + str(int(time.time() * 1000))[3:15]
    cur.execute(f"EXPLAIN PLAN SET STATEMENT_ID='{sid}' FOR {sql}")
    cur.execute("SELECT cardinality FROM plan_table WHERE statement_id=:s AND "
                "cardinality IS NOT NULL ORDER BY id ASC", {"s": sid})
    r = cur.fetchone()
    try:
        cur.execute("DELETE FROM plan_table WHERE statement_id=:s", {"s": sid})
    except Exception:
        pass
    return int(r[0]) if r else None


def build(tname, n, clusters):
    """clusters: list of tuples of column names, each an independent correlated
    cluster (all cols 0/1; first~Bern(0.5); rest follow base with prob gamma)."""
    rng = np.random.default_rng(20260904)
    cols = dict()  # name -> np array
    # build per cluster: first col ~ Bern(0.5); each subsequent col follows the
    # first with prob gamma (high correlate), flipped otherwise.
    gamma = 0.93
    for cl in clusters:
        base = rng.binomial(1, 0.5, n).astype(float)
        cols[cl[0]] = base
        for c in cl[1:]:
            # follow the cluster base with prob gamma
            flip = rng.binomial(1, 1.0 - gamma, n)
            vals = np.where(flip == 1, 1.0 - base, base)
            cols[c] = vals
    allc = [c for cl in clusters for c in cl]
    con = conn()
    cur = con.cursor()
    try:
        cur.execute(f"DROP TABLE {tname} PURGE")
    except oracledb.DatabaseError:
        pass
    cur.execute(f"CREATE TABLE {tname} (" +
                ",".join(f"{c} NUMBER" for c in allc) + ")")
    rows = list(zip(*[cols[c].astype(int).tolist() for c in allc]))
    cur.executemany(f"INSERT INTO {tname} VALUES ("
                    + ",".join(f":{i}" for i in range(1, len(allc) + 1))
                    + ")", rows, batcherrors=True)
    con.commit()
    con.close()
    return allc


def run_scenario(name, clusters, deploy_a, deploy_b):
    tname = "GC6_" + re.sub(r"\W", "", name).upper()[:10]
    allc = build(tname, 1_000_000, clusters)
    Q = (f"SELECT * FROM {tname} WHERE " +
         " AND ".join(f"{c}=1" for c in allc))
    QCNT = (f"SELECT COUNT(*) FROM {tname} WHERE " +
            " AND ".join(f"{c}=1" for c in allc))
    con = conn()
    cur = con.cursor()
    # count helper bound to this table
    def cnt(cur, tname, cond):
        cur.execute(f"SELECT COUNT(*) FROM {tname} WHERE {cond}")
        return cur.fetchone()[0]
    truth = cnt(cur, tname, " AND ".join(f"{c}=1" for c in allc))
    total = cnt(cur, tname, "1=1")
    # per-cluster joint counts for independence expectation
    prod = 1.0
    for cl in clusters:
        prod *= cnt(cur, tname, " AND ".join(f"{c}=1" for c in cl))
    composed = prod / (total ** (len(clusters) - 1))

    def gather(groups):
        if groups:
            parts = ["FOR ALL COLUMNS SIZE AUTO"] + \
                [f"FOR COLUMNS ({','.join(g)}) SIZE 1000" for g in groups]
            cur.execute("BEGIN DBMS_STATS.GATHER_TABLE_STATS("
                        f"ownname=>'SYSTEM', tabname=>'{tname}', "
                        "method_opt=>:m, estimate_percent=>100, degree=>1); END;",
                        {"m": " ".join(parts)})
        else:
            cur.execute("BEGIN DBMS_STATS.GATHER_TABLE_STATS("
                        f"ownname=>'SYSTEM', tabname=>'{tname}', "
                        "method_opt=>'FOR ALL COLUMNS SIZE AUTO', "
                        "estimate_percent=>100, degree=>1); END;")

    print(f"\n==== {name} : 6 conj = {allc} ; TRUE={truth} ; "
          f"indep-composed~={composed:.0f} ====")
    rec = {"scenario": name, "truth": truth, "composed_expect": round(composed, 1)}
    for label, grps in [("none", []), ("3x2col(A)", deploy_a),
                        ("2x3col(B)", deploy_b), ("full-6col", [tuple(allc)])]:
        gather(grps)
        e = explain_rows(cur, Q)
        ratio = (float(e) / truth) if truth and e else float("nan")
        rec[label] = e
        print(f"  {label:<10} E-Rows={str(e):>10}  vs truth={ratio:6.3f}")
    try:
        cur.execute(f"DROP TABLE {tname} PURGE")
    except oracledb.DatabaseError:
        pass
    con.commit()
    con.close()
    return rec


def main():
    out = []
    # Scenario I: 3 independent 2-col clusters
    s1 = run_scenario(
        "three_2col_clusters",
        [("A", "B"), ("C", "D"), ("E", "F")],
        deploy_a=[("A", "B"), ("C", "D"), ("E", "F")],   # 3x2col = correct
        deploy_b=[("A", "B", "C"), ("D", "E", "F")],     # 2x3col = cross-cut
    )
    # Scenario II: 2 independent 3-col clusters
    s2 = run_scenario(
        "two_3col_clusters",
        [("A1", "B1", "C1"), ("D1", "E1", "F1")],
        deploy_a=[("A1", "B1"), ("C1", "D1"), ("E1", "F1")],  # 3x2col = wrong-ish
        deploy_b=[("A1", "B1", "C1"), ("D1", "E1", "F1")],    # 2x3col = correct
    )
    out = [s1, s2]
    Path_ = __import__("pathlib").Path("results/oracle_composition_6way.json")
    Path_.write_text(json.dumps(out, indent=2))
    print(f"\n-> results/oracle_composition_6way.json")


if __name__ == "__main__":
    main()
