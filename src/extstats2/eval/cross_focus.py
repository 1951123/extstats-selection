"""Cross-backend focused validation (M4) — dominant correlated column-group.

On census queries with genuinely high *single-column* baseline q-error (the
multi-column strongly-correlated / very-sparse ones), PG and Oracle are each
asked, independently and from their *natural* per-column statistics, which 2-
column mcv column-group most repairs the query. M4 shows the answer is a data
property, not an engine property:

  query.62   truth 45    : PG 2045 -> (iRspouse,iWork89) 4.1  | Oracle
                           2069 -> (iRspouse,iWork89) 2.5        [same pair]
  query.184  truth 13    : both -> (iDisabl1,iRspouse)
  query.61   truth 107   : both -> (iDisabl2,iYearsch)

The non-dominant pairs fail to help on *both* engines (stay ~1000-4000), which
is the cross-backend counterpart of v1's one-stat sufficiency.

Census CLIMATE = Oracle SYSTEM.CLIMATE = 2,458,285 rows (identical data). To be
fair, "no extended statistics" is measured from each engine's natural per-column
statistics (PG ANALYZE; Oracle GATHER 'FOR ALL COLUMNS SIZE AUTO' at 100%) and
each column-group is gathered at the engine's full-scan capacity (Oracle
estimate_percent=100); no cross-backend tuning is applied.

Usage::

    python -m extstats2.eval.cross_focus --out results/cross_focus.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

from ..config import DBConfig, get_backend
from ..core.candidates import generate_candidates_per_query
from ..core.measure import measure_query

_PG = DBConfig(host="localhost", port=5432, user="postgres",
               password="postgres", dbname="census")
_OR = DBConfig(host="localhost", port=1521, user="SYSTEM",
               password="lxf82073077", service="FREEPDB1")

# (census 0-based index, query label, truth, [pairs to test])
# The pairs are the PG+Oracle-verified dominant one plus 2 non-dominant decoys.
_CASES = [
    {"idx": 61,  "label": "query.62",
     "pairs": [("iRspouse", "iWork89"), ("dTravtime", "iRspouse"),
               ("iClass", "iRspouse")]},
    {"idx": 183, "label": "query.184",
     "pairs": [("iDisabl1", "iRspouse"), ("dRpincome", "iDisabl1"),
               ("dDepart", "iRspouse")]},
    {"idx": 60,  "label": "query.61",
     "pairs": [("iDisabl2", "iYearsch"), ("iDisabl2", "iSubfam2"),
               ("iLooking", "iSept80")]},
]


def _load_queries():
    from ..bench import load_benchmark
    return load_benchmark("census")


def _pg_case(pg, q, pairs):
    for s in list(pg.list_stats(".climate")):
        pg.drop_stat(s)
    pg.restore_natural_stats(".climate")
    res = {"base": pg.estimate(q).qerror, "pairs": {}}
    cap = 0
    cands = [c for c in generate_candidates_per_query([q], arities=(2,))[q.qid]]
    # only test the requested pairs to be symmetric with Oracle cost
    wanted = {tuple(sorted(p)) for p in pairs}
    sub = [c for c in cands if tuple(sorted(c.columns)) in wanted]
    meas = measure_query(pg, q, sub, capacity_levels=(cap,), capabilities=["mcv"])
    for ck, cm in meas.candidates.items():
        res["pairs"][tuple(sorted(cm.columns))] = cm.levels[cap]["qerror"]
    return res


def _oracle_case(orc, q, pairs):
    from extstats2.backend.base import StatObject
    from ..backend.capabilities import Capacity
    mcv = [c for c in orc.supported_capabilities() if c.name == "mcv"][0]
    for s in list(orc.list_stats(".climate")):
        orc.drop_stat(s)
    orc.restore_natural_stats(".climate", estimate_percent=100.0)
    res = {"base": orc.estimate(q).qerror, "pairs": {}}
    for cols in pairs:
        for s in list(orc.list_stats(".climate")):
            orc.drop_stat(s)
        obj = StatObject(table=".climate", columns=cols, capability=mcv,
                         capacity=Capacity(2))  # estimate_percent=100
        orc.build_stats([obj], Capacity(2))
        e = orc.estimate(q)
        res["pairs"][tuple(sorted(cols))] = e.qerror
    for s in list(orc.list_stats(".climate")):
        orc.drop_stat(s)
    orc.restore_natural_stats(".climate", estimate_percent=100.0)
    return res


def run(pg, orc, queries) -> dict:
    out = {}
    for case in _CASES:
        q = queries[case["idx"]]
        p = _pg_case(pg, q, case["pairs"])
        s = _oracle_case(orc, q, case["pairs"])
        rows = []
        for cols in case["pairs"]:
            k = tuple(sorted(cols))
            rows.append({"columns": list(cols),
                         "postgres_qerror": _fmt(p["pairs"].get(k)),
                         "oracle_qerror": _fmt(s["pairs"].get(k))})
        # dominant pair agreement
        def best(res):
            bestk, bestv = None, 1e18
            for k, v in res["pairs"].items():
                if v is not None and v < bestv:
                    bestk, bestv = k, v
            return list(bestk) if bestk else None
        out[case["label"]] = {
            "truth": q.ground_truth,
            "postgres_base": _fmt(p["base"]),
            "oracle_base": _fmt(s["base"]),
            "postgres_dominant_pair": best(p),
            "oracle_dominant_pair": best(s),
            "agreement": best(p) == best(s),
            "rows": rows,
        }
    return out


def _fmt(x):
    if x is None:
        return None
    return round(float(x), 3)


def _main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default=None)
    args = ap.parse_args(argv)
    queries = _load_queries()
    pg = get_backend("postgres", cfg=_PG)
    orc = get_backend("oracle", cfg=_OR)
    out = run(pg, orc, queries)
    if args.out:
        p = Path(args.out)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(out, indent=2))
        print(f"[cross_focus] wrote {p}")
    print("\n=== CROSS-BACKEND DOMINANT-PAIR AGREEMENT (M4) ===")
    for label, obj in out.items():
        print(f"\n{label} (truth={obj['truth']})  base: PG={obj['postgres_base']} "
              f"Oracle={obj['oracle_base']}")
        print(f"  dominant pair PG={obj['postgres_dominant_pair']}  "
              f"Oracle={obj['oracle_dominant_pair']}  agreement={obj['agreement']}")
        for r in obj["rows"]:
            print(f"    {r['columns']}: PG qerr={r['postgres_qerror']}  "
                  f"Oracle qerr={r['oracle_qerror']}")
    return 0


if __name__ == "__main__":
    sys.exit(_main())
