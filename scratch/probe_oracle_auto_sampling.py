"""Probe: Oracle AUTO_SAMPLE_SIZE default vs our current 1% L0 ladder.

We decided Oracle how-much = fully engine AUTO (no λ grid). Our current
measurement corpora were gathered at estimate_percent=1 (ad-hoc shallow
ladder-L0). Oracle's documented default for gather_table_stats.estimate_percent
is DBMS_STATS.AUTO_SAMPLE_SIZE (let-the-engine-choose). This probe compares, on
representative tables (CLIMATE large ~2.46M; POSTS small ~92K; TAGS tiny), the
*recorded* sample size / sampled-%, realised column-group buckets, and wall
time at estimate_percent=1 vs AUTO_SAMPLE_SIZE (constant value 0).

Always restores each table to AUTO natural stats at the end (no corruption).

Usage:  python scratch/probe_oracle_auto_sampling.py        # all rep tables
"""
from __future__ import annotations

import time

import oracledb

CFG = dict(user="SYSTEM", password="lxf82073077", dsn="localhost:1521/FREEPDB1")


def main():
    c = oracledb.connect(**CFG)
    c.autocommit = True
    cur = c.cursor()

    def sample(cur, tab):
        cur.execute(
            "SELECT NUM_ROWS, SAMPLE_SIZE FROM USER_TAB_STATISTICS "
            "WHERE table_name=:t", {"t": tab})
        r = cur.fetchone()
        if not r or r[0] is None:
            return None
        n, s = r
        return n, s, (100.0 * s / n) if n else float("nan")

    def hist(cur, tab):
        cur.execute(
            "SELECT column_name, histogram, num_buckets "
            "FROM USER_TAB_COL_STATISTICS WHERE table_name=:t "
            "AND column_name LIKE 'SYS_ST%' ORDER BY 1", {"t": tab})
        return [(x[0], x[1], x[2]) for x in cur.fetchall()]

    def gather(cur, tab, mo, ep):
        cur.execute(
            "BEGIN DBMS_STATS.GATHER_TABLE_STATS("
            "ownname=>'SYSTEM', tabname=>:t, method_opt=>:m, "
            "estimate_percent=>:e, degree=>1); END;",
            {"t": tab, "m": mo, "e": ep})

    def drop_grp(cur, tab, grp):
        cur.execute(
            "BEGIN DBMS_STATS.DROP_EXTENDED_STATS("
            "ownname=>'SYSTEM', tabname=>:t, extension=>:e); END;",
            {"t": tab, "e": grp})

    reps = [
        ("Census CLIMATE", "CLIMATE"),
        ("stats_CEB POSTS", "POSTS"),
        ("stats_CEB TAGS", "TAGS"),
        ("stats_CEB USERS", "USERS"),
    ]

    for name, tab in reps:
        cur.execute(
            "SELECT column_name FROM USER_TAB_COLUMNS WHERE table_name=:t "
            "AND ROWNUM<=2 ORDER BY column_id", {"t": tab})
        cols = [x[0] for x in cur.fetchall()]
        if len(cols) < 2:
            print(f"[{name}] <2 cols; skip"); continue
        grp = "(" + ",".join(cols) + ")"
        mo = f"FOR ALL COLUMNS SIZE AUTO FOR COLUMNS {grp} SIZE 254"

        print(f"== {name} ({tab}) pair={cols} ==")

        t0 = time.time(); gather(cur, tab, mo, 1); d1 = time.time() - t0
        s1, h1 = sample(cur, tab), hist(cur, tab)
        print(f"  1%   : {s1}  group_buckets={h1}  {d1:5.1f}s")

        t0 = time.time(); gather(cur, tab, mo, 0); da = time.time() - t0
        # NB 0 == DBMS_STATS.AUTO_SAMPLE_SIZE
        sa, ha = sample(cur, tab), hist(cur, tab)
        print(f"  AUTO : {sa}  group_buckets={ha}  {da:5.1f}s")

        # restore table to clean AUTO natural (no column group)
        drop_grp(cur, tab, grp)
        gather(cur, tab, "FOR ALL COLUMNS SIZE AUTO", 0)
        print()

    c.close()


if __name__ == "__main__":
    main()
