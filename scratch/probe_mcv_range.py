"""Micro-probe: does PostgreSQL MCV extended statistics help (or engage) on
two-column RANGE predicates? Answers the question empirically on the posts
table (numeric score, viewcount). For each query it reports, in strict order:
  baseline (stat dropped + analyze)  -> planner row estimate
  with-mcv (mcv created + analyze)    -> planner row estimate
and the true count. Assumes DB == clone of `stats` named rp2 (or --db).
"""
import argparse
import json
import subprocess
import sys

DB = "rp2"


def psql(sql):
    out = subprocess.run(
        ["psql", "-h", "localhost", "-U", "postgres", "-d", DB,
         "-P", "pager=off", "-At", "-c", sql],
        capture_output=True, text=True,
        env={**__import__("os").environ, "PGPASSWORD": "postgres"})
    return out.stdout.strip() or out.stderr.strip()


def explain_rows(sql):
    j = json.loads(psql(f"EXPLAIN (FORMAT JSON) {sql}"))
    return j[0]["Plan"]["Plan Rows"]


def true_count(where):
    return int(psql(f"SELECT count(*) FROM posts WHERE {where}").splitlines()[-1])


QUERIES = [
    ("HIGH tail (corr-heavy): score>50 AND viewcount>5000", "score>50 AND viewcount>5000"),
    ("MIDDLE wide: score BETWEEN 0 AND 30 AND viewcount BETWEEN 0 AND 500",
     "score BETWEEN 0 AND 30 AND viewcount BETWEEN 0 AND 500"),
    ("LOW dup (negative score): score<0 AND viewcount>10000", "score<0 AND viewcount>10000"),
]


def main(db):
    global DB
    DB = db
    psql("ANALYZE posts")
    for desc, where in QUERIES:
        true = true_count(where)
        # ensure clean: drop stat if present
        psql("DROP STATISTICS IF EXISTS pr_mcv")
        psql("ANALYZE posts")
        base = explain_rows(f"SELECT * FROM posts WHERE {where}")
        psql("CREATE STATISTICS pr_mcv(ndistinct, dependencies, mcv) "
             "ON score, viewcount FROM posts")
        psql("ANALYZE posts")
        with_mcv = explain_rows(f"SELECT * FROM posts WHERE {where}")
        def qr(e):
            return round(max(e / true, true / e), 2)
        print(f"\n[{desc}]  TRUE={true}")
        print(f"   baseline(no stat) rows={base:>10}  qerr={qr(base)}")
        print(f"   with mcv          rows={with_mcv:>10}  qerr={qr(with_mcv)}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="rp2")
    a = ap.parse_args()
    main(a.db)
