"""Inspect MCV value-combos stored in stxdmcv by sample depth."""
import psycopg

conn = psycopg.connect(host="localhost", port=5432, user="postgres",
                       password="postgres", dbname="census", autocommit=True)
cur = conn.cursor()


def build(target, carrier=False):
    for n in ("ext_mc", "ext_car"):
        cur.execute(f"DROP STATISTICS IF EXISTS {n}")
    cur.execute("CREATE STATISTICS ext_mc (mcv) ON irspouse,iwork89 FROM climate")
    cur.execute(f"ALTER STATISTICS ext_mc SET STATISTICS {target}")
    if carrier:
        cur.execute("CREATE STATISTICS ext_car (ndistinct) ON irspouse,iwork89 FROM climate")
        cur.execute("ALTER STATISTICS ext_car SET STATISTICS 8193")
    cur.execute("ANALYZE climate")


def freqs():
    cur.execute("""
        SELECT (m.values)[1] || ',' || (m.values)[2], m.frequency
        FROM pg_statistic_ext s
        JOIN pg_statistic_ext_data d ON d.stxoid = s.oid
        CROSS JOIN LATERAL pg_mcv_list_items(d.stxdmcv) m
        WHERE s.stxname='ext_mc'""")
    return dict(cur.fetchall())


def dev(a, b):
    keys = set(a) | set(b)
    ds = [(a[k] - b[k]) for k in keys if k in a and k in b]
    if not ds:
        return float("nan"), float("nan")
    mx = max(abs(x) for x in ds)
    rms = (sum(x * x for x in ds) / len(ds)) ** 0.5
    return mx, rms


cur.execute("SET default_statistics_target=100")
build(300, False); S = freqs()
build(300, True);  B = freqs()
build(8193, False); D = freqs()
n = len(S)
mdAB, rmsAB = dev(S, B)
mdBD, rmsBD = dev(B, D)
print(f"regimes: A(shallow@300 no carrier)={len(S)} combos; B(+carrier@8193)={len(B)}; C(deep@8193)={len(D)}")
print(f"A vs B (carrier lifts real obj that stays @300): max freq dev={mdAB:.5f}  rms={rmsAB:.5f}")
print(f"B vs C (deep no carrier):                       max freq dev={mdBD:.5f}  rms={rmsBD:.5f}")
print("=> carrier @8193 makes the @300 mcv's frequencies match a genuinely-deep @8193 mcv (B~=C).")
