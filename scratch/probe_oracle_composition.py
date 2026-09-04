"""Oracle multi-group composition factorial (none/AB/CD/AB+CD/ABCD).

Directly answers: does Oracle 23ai CBO compose TWO DISJOINT correlated column
groups for ONE AND query, i.e. does Sel(a,b,c,d) ~= Sel(a,b) x Sel(c,d)?
ChatGPT + Oracle public docs: this is NOT documented / closed-source optimizer
behavior => determined empirically. This probe is the experiment.

Controlled synthetic table (fresh, in SYSTEM schema, deterministic):
  T_GC(a,b,c,d) ; a,b in {0,1} correlated cluster; c,d in {0,1} correlated
  cluster; (a,b) FIXED independent of (c,d). N=1,000,000.
  Query Q: WHERE a=1 AND b=1 AND c=1 AND d=1
  Expect under composition: E(AB+CD) ~= truth = N*P(a=1&b=1)*P(c=1&d=1).
  If Oracle does NOT compose, AB+CD will behave like one dominant group only.

Compare EXPLAIN E-Rows under stat configs:
  0 none ; 1 (a,b) ; 2 (c,d) ; 3 (a,b)+(c,d) ; 4 (a,b,c,d)  [single combined gather]

Usage: .venv/bin/python -u scratch/probe_oracle_composition.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np
import oracledb

sys.path.insert(0, "src")

OR = dict(user="SYSTEM", password="lxf82073077",
          dsn="localhost:1521/FREEPDB1")
TNAME = "GC_COMP_PROBE"
N = 1_000_000


def conn():
    return oracledb.connect(**OR)


def build_table() -> None:
    # correlated clusters: P(b=1|a=1)=0.95, P(b=1|a=0)=0.05 ; P(d=1|c=1)=0.95,
    # P(d=1|c=0)=0.05 ; (a,b) independent of (c,d) ; marginals all ~0.5
    rng = np.random.default_rng(20260904)
    a = rng.binomial(1, 0.5, N)
    b = np.where(a == 1,
                 rng.binomial(1, 0.95, N), rng.binomial(1, 0.05, N))
    c = rng.binomial(1, 0.5, N)
    d = np.where(c == 1,
                 rng.binomial(1, 0.95, N), rng.binomial(1, 0.05, N))
    con = conn()
    cur = con.cursor()
    try:
        cur.execute(f"DROP TABLE {TNAME} PURGE")
    except oracledb.DatabaseError:
        pass
    cur.execute(f"CREATE TABLE {TNAME} (a NUMBER, b NUMBER, c NUMBER, d NUMBER)")
    # bulk insert via executemany in chunks
    rows = list(zip(a.tolist(), b.tolist(), c.tolist(), d.tolist()))
    cur.executemany(f"INSERT INTO {TNAME} VALUES (:1,:2,:3,:4)", rows,
                    batcherrors=True)
    con.commit()
    print(f"[syn] built {TNAME} N={N}", flush=True)
    con.close()


def truth(cur, sql):
    cur.execute(sql)
    return cur.fetchone()[0]


def explain_rows(cur, sql):
    sid = "GC" + str(int(time.time() * 1000))[:12]
    cur.execute(f"EXPLAIN PLAN SET STATEMENT_ID='{sid}' FOR {sql}")
    cur.execute("SELECT cardinality FROM plan_table WHERE statement_id=:s "
                "AND cardinality IS NOT NULL ORDER BY id ASC", {"s": sid})
    r = cur.fetchone()
    try:
        cur.execute("DELETE FROM plan_table WHERE statement_id=:s", {"s": sid})
    except Exception:
        pass
    return int(r[0]) if r else None


def main() -> None:
    build_table()  # create + populate the synthetic table FIRST
    ND = 2458  # just a constant not used; real N used below via truth
    rec = {}
    Q = f"SELECT * FROM {TNAME} WHERE a=1 AND b=1 AND c=1 AND d=1"
    QCNT = f"SELECT COUNT(*) FROM {TNAME} WHERE a=1 AND b=1 AND c=1 AND d=1"
    con = conn()
    cur = con.cursor()
    t = truth(cur, QCNT)
    print(f"[syn] TRUE 4-way cardinality = {t}")
    rec["truth"] = t

    def gather(groups, size=1000):
        """One combined GATHER creating the listed column groups."""
        if groups:
            parts = ["FOR ALL COLUMNS SIZE AUTO"] + \
                [f"FOR COLUMNS ({','.join(g)}) SIZE {size}" for g in groups]
            cur.execute(
                "BEGIN DBMS_STATS.GATHER_TABLE_STATS("
                f"ownname=>'SYSTEM', tabname=>'{TNAME}', method_opt=>:m, "
                "estimate_percent=>100, degree=>1); END;", {"m": " ".join(parts)})
        else:
            cur.execute(
                "BEGIN DBMS_STATS.GATHER_TABLE_STATS("
                f"ownname=>'SYSTEM', tabname=>'{TNAME}', "
                "method_opt=>'FOR ALL COLUMNS SIZE AUTO', "
                "estimate_percent=>100, degree=>1); END;")

    def groups_live():
        cur.execute("SELECT extension FROM user_stat_extensions "
                    "WHERE table_name=:t", {"t": TNAME})
        import re
        return [tuple(x.strip('"').upper() for x in
                      re.findall(r'"([^"]+)"', str(r[0]))) for r in cur.fetchall()]

    configs = [
        ("none",           []),
        ("AB",             [("A", "B")]),
        ("CD",             [("C", "D")]),
        ("AB+CD",          [("A", "B"), ("C", "D")]),
        ("ABCD",           [("A", "B", "C", "D")]),
    ]
    print(f"{'config':<8}{'E-Rows':>12}{'vs truth':>12}")
    for name, grps in configs:
        gather(grps)
        e = explain_rows(cur, Q)
        ratio = (float(e) / t) if t and e else float("nan")
        rec[name] = e
        print(f"{name:<8}{str(e):>12}{ratio:>12.3f}")
    # report EXPECTED multiplicative truth if composed
    # P(AB): count a=1&b=1 ; P(CD): count c=1&d=1
    pab = truth(cur, f"SELECT COUNT(*) FROM {TNAME} WHERE a=1 AND b=1")
    pcd = truth(cur, f"SELECT COUNT(*) FROM {TNAME} WHERE c=1 AND d=1")
    tot = truth(cur, f"SELECT COUNT(*) FROM {TNAME}")
    abcd_composed = pab * pcd / tot
    print(f"[syn] P(AB)={pab} P(CD)={pcd} total={tot} => INDEP-composed "
          f"expectation = {abcd_composed:.1f}  (truth={t})")
    rec["pab"], rec["pcd"], rec["total"], rec["composed_expect"] = \
        pab, pcd, tot, round(abcd_composed, 1)
    # clean up
    try:
        cur.execute(f"DROP TABLE {TNAME} PURGE")
    except oracledb.DatabaseError:
        pass
    con.commit()
    con.close()
    out = Path("results/oracle_composition_factorial.json")
    out.write_text(__import__("json").dumps(rec, indent=2))
    print(f"[syn] results -> {out}")


if __name__ == "__main__":
    main()
