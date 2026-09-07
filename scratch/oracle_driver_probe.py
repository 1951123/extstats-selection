"""Discover q.184's true dominant pair on Oracle, then sweep SIZE on it.

FAST VERSION: discovery is done at L1 (estimate_percent 10, ~2 s/gather) because
"which pair is the driver" is a relative ranking that shallow sampling already
stabilizes for genuinely correlated pairs; only the winner's SIZE saturation
curve is re-measured at deep L2 (100%, deterministic) to read absolute q-error /
realized buckets. Probe pairs are drawn from q.184's actual predicates.

Note SIZE>=e.g. 10000 is an illegal Oracle method_opt literal (ORA-20000), so we
cap the sweep at 1000.
"""
from extstats2.bench import load_benchmark
from extstats2.config import DBConfig, get_backend
from extstats2.backend.base import StatObject
from extstats2.backend.capabilities import Capacity
from extstats2.backend.oracle import _text, _parse_extension_expression

OR = DBConfig(host="localhost", port=1521, user="SYSTEM",
              password="lxf82073077", service="FREEPDB1")
be = get_backend("oracle", cfg=OR)
for s in list(be.list_stats(".climate")):
    be.drop_stat(s)

Q = load_benchmark("census")
q = next(x for x in Q if x.qid == "query.184")
mcv = next(c for c in be.supported_capabilities() if c.name == "mcv")
TQ = be._q_table(".climate")

def hidden_buckets(cols):
    want = be._q_cols(cols)
    with be._cur() as cur:
        cur.execute("SELECT extension, extension_name FROM user_stat_extensions "
                    "WHERE table_name=:t", {"t": TQ})
        hidden = next((_text(nm) for expr, nm in cur.fetchall()
                       if tuple(_parse_extension_expression(_text(expr))) == want),
                      None)
    if hidden is None:
        return None
    with be._cur() as cur:
        cur.execute("SELECT num_buckets FROM user_tab_col_statistics "
                    "WHERE table_name=:t AND column_name=:c", {"t": TQ, "c": hidden})
        r = cur.fetchone()
    return int(r[0]) if r else None

def build_measure(cols, size, level):
    obj = StatObject(table=".climate", columns=list(cols), capability=mcv,
                     capacity=Capacity(level), name=f"disc_l{level}_{size}")
    be.create_stat(obj)
    try:
        be.build_stat_param(obj, size)
        nb = hidden_buckets(cols)
        est = be.estimate(q)
        return nb, est.estimate, est.qerror
    finally:
        be.drop_stat(obj)

disco_pairs = [
    ("iDisabl1", "iRspouse"),
    ("dDepart", "dRpincome"),
    ("iRelat2", "dRpincome"),
    ("iDisabl1", "iImmigr"),
    ("iEnglish", "iImmigr"),
    ("dTravtime", "dRpincome"),
    ("iRspouse", "dTravtime"),
    ("dDepart", "iDisabl1"),
]

# ---- discovery at L1 (fast) ----
be.enter_sampling_state(".climate", 1)
b1 = be.estimate(q).qerror
print(f"q.184 truth={q.ground_truth} | L1 no-ext baseline qerr={b1:.1f}")
print("\n== discovery (SIZE 254 @ L1, fast) ==")
print(f"{'pair':<26}{'buckets':>9}{'est':>10}{'qerr':>9}{'vs base':>9}")
results = []
for a, b in disco_pairs:
    try:
        nb, est, qerr = build_measure((a, b), 254, 1)
    except Exception as e:
        print(f"{('('+a+','+b+')'):<26} ERROR {str(e)[:40]}")
        continue
    imp = (b1 - qerr) / b1 * 100
    results.append(((a, b), nb, est, qerr, imp))
    print(f"{('('+a+','+b+')'):<26}{nb:>9}{est:>10}{qerr:>9.1f}{imp:>8.2f}%")

results.sort(key=lambda r: r[3])
winners = [r for r in results[:2]]
print("\nTOP driver pairs (L1):")
for (p, nb, est, qerr, imp) in winners:
    print(f"  {p}  qerr={qerr:.1f} (imp {imp:.2f}%)")

# ---- sweep SIZE at deep L2 on the top-1 driver ----
wp = winners[0][0]
print(f"\nWINNER driver pair = {wp}; sweep SIZE @ L2")
be.enter_sampling_state(".climate", 2)
b2 = be.estimate(q).qerror
print(f"L2 no-ext baseline qerr={b2:.1f}")
print(f"{'SIZE p':>7}{'buckets':>12}{'est':>11}{'qerr':>9}")
for p in [25, 50, 100, 254, 500, 1000]:
    try:
        nb, est, qerr = build_measure(wp, p, 2)
    except Exception as e:
        print(f"{p:>7}  ERROR {str(e)[:50]}")
        break
    print(f"{p:>7}{nb:>12}{est:>11}{qerr:>9.2f}")

for s in list(be.list_stats(".climate")):
    be.drop_stat(s)
print("DRIVER PROBE (fast) DONE")
