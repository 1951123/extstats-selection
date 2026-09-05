"""Probe: at Oracle L0 (1% sample), does SIZE 64 vs SIZE 254 realize DIFFERENT
actual bucket counts, or do they collapse to the same (usually a small, NDV/thin
sample-capped) count?

If realized buckets are the same (<~64, driven by NDV or the thin 1% sample), then
offering both 64 and 254 at L0 is redundant -> the fix cost would be wasted and the
grid collapses. If they differ, the 64-vs-254 axis is meaningful at L0.

Reads back USER_TAB_COL_STATISTICS.num_buckets for the hidden extension column at
each SIZE, on the real Census CLIMATE table.

Usage: .venv/bin/python -u scratch/probe_oracle_l0_bucket_realized.py
"""
from __future__ import annotations
import sys, re
sys.path.insert(0, "src")
from extstats2.config import DBConfig, get_backend
from extstats2.backend.base import StatObject
from extstats2.backend.capabilities import Capacity
from extstats2.backend.oracle import _text, _parse_extension_expression

OR = DBConfig(host="localhost", port=1521, user="SYSTEM",
              password="lxf82073077", service="FREEPDB1")
be = get_backend("oracle", cfg=OR)

PAIRS = [
    ("iRspouse", "iWork89"),     # q.62 driver (saw 254 WORSE -> suggests capped/low NDV)
    ("iDisabl1", "iRspouse"),    # q.184 driver (rare, truth 13)
    ("dDepart", "dRpincome"),    # a broader-range / continuous-ish pair (not driver, high NDV?)
    ("dIncome3", "iRelat2"),
]

def realized(cols, size):
    o = StatObject(table=".climate", columns=cols, capability=None,
                   capacity=Capacity(0), name=f"b_{size}")
    # build at L0 1% sample with explicit SIZE
    be.build_stat_param(o, size)
    tq = be._q_table(".climate")
    want = tuple(be._q_cols(cols))
    hidden = None
    with be._cur() as cur:
        cur.execute("SELECT extension_name, extension FROM user_stat_extensions "
                    "WHERE table_name=:t", {"t": tq})
        for nm, expr in cur.fetchall():
            if tuple(_parse_extension_expression(_text(expr))) == want:
                hidden = _text(nm); break
    nb = None
    if hidden:
        with be._cur() as cur:
            cur.execute("SELECT num_buckets FROM user_tab_col_statistics "
                        "WHERE table_name=:t AND column_name=:c", {"t": tq, "c": hidden})
            r = cur.fetchone(); nb = int(r[0]) if r else None
    be.drop_stat(o)
    return nb

# clean
be.restore_natural_stats(".climate", estimate_percent=1.0)
for s in list(be.list_stats(".climate")): be.drop_stat(s)
print("Oracle L0 (estimate_percent=1): realized bucket count vs requested SIZE")
print(f"{'pair':<26}{'SIZE64':>9}{'SIZE254':>9}   equal?")
for cols in PAIRS:
    try:
        p = ','.join(cols)
        b64 = realized(cols, 64)
        b254 = realized(cols, 254)
        print(f"({p}){'':<{22-len(p)}}{str(b64):>9}{str(b254):>9}   {b64==b254}")
    except Exception as e:
        print(','.join(cols), "ERR", str(e)[:60])
# clean up
be.restore_natural_stats(".climate", estimate_percent=100.0)
for s in list(be.list_stats(".climate")): be.drop_stat(s)
print("done/clean")
