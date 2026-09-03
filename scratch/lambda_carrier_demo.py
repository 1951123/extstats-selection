"""λ-carrier demonstration on PG census `climate`.

Claim: with a real ext stat (mcv) kept at a *shallow* target, adding a
λ-carrier (a cheap ndistinct ext stat at a deep target) forces the single
shared ANALYZE to scan deep, so a sparse correlated value-combo that the
shallow sample would MISS gets captured — i.e., the carrier raises effective
fidelity (lambda) for the real object WITHOUT changing the real object's own
parameter. This is the Direction-A 'free-rider' at the ext layer.

Instrument: correlated pair (irspouse, iwork89); rare value combo (x,y) with
tiny true count. Compare planner row-estimate of that conjunction across:
  A) real mcv at shallow target (no carrier)
  B) same mcv at the SAME shallow target + carrier forcing a deep scan
"""

import psycopg

DSN = dict(host="localhost", port=5432, user="postgres",
           password="postgres", dbname="census")
TBL = "climate"
PAIR = ("irspouse", "iwork89")

conn = psycopg.connect(autocommit=True, **DSN)
cur = conn.cursor()


def clean():
    cur.execute(f"SELECT stxname FROM pg_statistic_ext WHERE stxrelid='{TBL}'::regclass")
    for (n,) in cur.fetchall():
        cur.execute(f"DROP STATISTICS IF EXISTS {n}")
    # pin single columns at default 100 (matching our deployment decision)
    cur.execute("SET default_statistics_target = 100")
    cur.execute(f"ANALYZE {TBL}")


def explain_est(where):
    sql = f"SELECT count(*) FROM {TBL} WHERE {where}"
    cur.execute(f"EXPLAIN (FORMAT JSON) {sql}")
    return int(cur.fetchone()[0][0]["Plan"]["Plan Rows"])


def true_count(where):
    cur.execute(f"SELECT count(*) FROM {TBL} WHERE {where}")
    return cur.fetchone()[0]


def run():
    clean()
    # Locate a sparse value-combo on the pair
    cur.execute(
        f'SELECT "{PAIR[0]}", "{PAIR[1]}", count(*) FROM {TBL} '
        f'GROUP BY 1,2 HAVING count(*) BETWEEN 1 AND 60 ORDER BY 3 ASC LIMIT 1')
    a, b, _ = cur.fetchone()
    where = f'"{PAIR[0]}" = {a} AND "{PAIR[1]}" = {b}'
    truth = true_count(where)
    mcv = f"ext_m_{PAIR[0]}_{PAIR[1]}"
    print(f"probe combo: {PAIR[0]}={a} AND {PAIR[1]}={b}   (true count = {truth} of 2.46M)")

    # ---- Regime A: real mcv shallow, no carrier ----
    clean()
    cur.execute(f"CREATE STATISTICS {mcv} (mcv) ON {PAIR[0]},{PAIR[1]} FROM {TBL}")
    cur.execute(f"ALTER STATISTICS {mcv} SET STATISTICS 300")  # ~300*300=90k sample
    cur.execute(f"ANALYZE {TBL}")
    estA = explain_est(where)
    print(f"[A] mcv@300 (no carrier):    estimate={estA}   err={max(estA/truth, truth/estA):.1f}x")

    # ---- Regime B: same mcv target + carrier forces deep scan ----
    cur.execute(f"CREATE STATISTICS ext_carrier (ndistinct) ON {PAIR[0]},{PAIR[1]} FROM {TBL}")
    cur.execute(f"ALTER STATISTICS ext_carrier SET STATISTICS 8193")  # ~full scan
    cur.execute(f"ANALYZE {TBL}")
    estB = explain_est(where)
    print(f"[B] mcv@300 + carrier@8193:   estimate={estB}   err={max(estB/truth, truth/estB):.1f}x")

    # ---- Regime C: ceiling - mcv itself deep, no carrier ----
    clean()
    cur.execute(f"CREATE STATISTICS {mcv} (mcv) ON {PAIR[0]},{PAIR[1]} FROM {TBL}")
    cur.execute(f"ALTER STATISTICS {mcv} SET STATISTICS 8193")
    cur.execute(f"ANALYZE {TBL}")
    estC = explain_est(where)
    print(f"[C] mcv@8193 (real deep):     estimate={estC}   err={max(estC/truth, truth/estC):.1f}x")

    print("\ninterpretation: A→B changes only by adding carrier (real mcv target UNCHANGED @300).")
    clean()


if __name__ == "__main__":
    run()
    print("done")
