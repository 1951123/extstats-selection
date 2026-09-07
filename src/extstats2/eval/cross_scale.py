"""LEGACY M4 scaled cross-backend validation (old capacity era; defaults to
Protocol-A at capacity level 0, statistics_target=100).  Historical / reference,
not the current S-grid evaluation path.

Scaled cross-backend validation (M4 — larger N, Protocol-A default).

Two stages:

1. **Baseline distribution** (cheap, one EXPLAIN per query per engine): over the
   full Census workload, measure from each engine's *natural* single-column
   statistics how many queries carry a genuinely large base q-error, and confirm
   the high-error sets coincide on PG and Oracle (correlation is a data property).

2. **Top-k one-column-group repairability (PG, Protocol-A)**: for the k worst
   queries by baseline, enumerate every 2-col mcv column-group on PostgreSQL and
   record (dominant pair, best repaired q-error). This is the core "one-stat
   sufficiency" statistic at scale. PostgreSQL protocol-A owns the enumeration
   (ANALYZE at default statistics_target=100 is cheap, ~0.25 s/stat, so no
   Protocol-M is needed here -- see architecture.md note about when Protocol-M
   pays off). Optionally re-verify PG's dominant pair for a few queries on Oracle
   (each Oracle full-scan column-group is ~22 s, so that stays a small spot-check).

Baselines use natural per-column stats (architecture.md §6.3a). Column groups are
built at the engine's full-scan capacity for accuracy on sparse correlated combos
(PG level2 target=10000; Oracle estimate_percent=100) -- but stage-2 PG uses
level0 (target=100) to keep the *enumeration* cheap; that still separates the
dominant pair from decoys on sparse Census queries at the magnitude level.

Usage::

    python -m extstats2.eval.cross_scale baseline --out results/cross_baseline.json
    python -m extstats2.eval.cross_scale topk --k 20 --oracle-spot 3 --out results/cross_topk.json
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

from ..config import DBConfig, get_backend
from ..core.candidates import generate_candidates_per_query

_PG = DBConfig(host="localhost", port=5432, user="postgres",
               password="postgres", dbname="census")
_OR = DBConfig(host="localhost", port=1521, user="SYSTEM",
               password="lxf82073077", service="FREEPDB1")

_REPAIR_TARGET = 5.0   # "repaired" = best one-group qerr below this


def _load_queries():
    from ..bench import load_benchmark
    return load_benchmark("census")


def _fmt(x):
    return None if x is None else round(float(x), 3)


# ---------------------------------------------------------------------------
# Stage 1 — baseline distribution
# ---------------------------------------------------------------------------

def stage_baseline() -> dict:
    queries = _load_queries()
    pg = get_backend("postgres", cfg=_PG)
    orc = get_backend("oracle", cfg=_OR)
    for s in list(pg.list_stats(".climate")):
        pg.drop_stat(s)
    pg.restore_natural_stats(".climate")
    for s in list(orc.list_stats(".climate")):
        orc.drop_stat(s)
    orc.restore_natural_stats(".climate", estimate_percent=100.0)
    pq: list[dict] = []
    oq: list[dict] = []
    for q in queries:
        pq.append({"qid": q.qid, "qerr": _fmt(pg.estimate(q).qerror)})
        oq.append({"qid": q.qid, "qerr": _fmt(orc.estimate(q).qerror)})
    d = {"n": len(queries), "per_query": {"postgres": pq, "oracle": oq}}
    pmap = {r["qid"]: r["qerr"] for r in pq}
    omap = {r["qid"]: r["qerr"] for r in oq}
    shared = sorted(set(pmap) & set(omap))
    d["stats"] = {
        "mean": {"postgres": _fmt_mean(list(pmap.values())),
                 "oracle": _fmt_mean(list(omap.values()))},
        "median": {"postgres": _fmt_median(list(pmap.values())),
                   "oracle": _fmt_median(list(omap.values()))},
    }
    lp = [math.log(pmap[q]) for q in shared if pmap[q] and omap.get(q)]
    lo = [math.log(omap[q]) for q in shared if omap[q] and pmap.get(q)]
    d["stats"]["log_pearson"] = _fmt_pearson(lp, lo)
    for thr in (1.0, 2.0, 5.0, 10.0, 100.0, 1000.0):
        hp = {q for q, v in pmap.items() if v and v >= thr}
        ho = {q for q, v in omap.items() if v and v >= thr}
        inter = len(hp & ho); union = len(hp | ho) or 1
        d.setdefault("thresholds", {})[str(thr)] = {
            "postgres": len(hp), "oracle": len(ho),
            "jaccard": round(inter / union, 3)}
    return d


def _fmt_mean(xs):
    if not xs:
        return None
    return round(float(sum(xs) / len(xs)), 3)


def _fmt_median(xs):
    if not xs:
        return None
    s = sorted(xs)
    m = len(s) // 2
    return round(float(s[m]), 3) if len(s) % 2 else \
        round(float((s[m - 1] + s[m]) / 2), 3)


def _fmt_pearson(a, b):
    import numpy as np
    return round(float(np.corrcoef(a, b)[0, 1]), 4)


# ---------------------------------------------------------------------------
# Stage 2 — PG top-k repairability (Protocol-A)
# ---------------------------------------------------------------------------

def _pg_repair(pg, q):
    """PG full 2-col enumeration; returns (base, dominant_pair, repaired)."""
    from ..core.measure import measure_query
    for s in list(pg.list_stats(".climate")):
        pg.drop_stat(s)
    pg.restore_natural_stats(".climate")
    base = pg.estimate(q).qerror
    cands = generate_candidates_per_query([q], arities=(2,))[q.qid]
    if not cands:
        return base, None, base
    res = measure_query(pg, q, cands, capacity_levels=(0,), capabilities=["mcv"])
    best = None
    for ck, cm in res.candidates.items():
        lv = cm.levels[0]
        if best is None or lv["qerror"] < best[1]:
            best = (cm.columns, lv["qerror"])
    repaired = best[1] if best else base
    return base, (best[0] if best else None), repaired


def _oracle_verify(orc, q, pair):
    """Build one full-sample column-group on Oracle; return (base, qerror)."""
    from ..backend.base import StatObject
    from ..backend.capabilities import Capacity
    mcv = [c for c in orc.supported_capabilities() if c.name == "mcv"][0]
    for s in list(orc.list_stats(".climate")):
        orc.drop_stat(s)
    orc.restore_natural_stats(".climate", estimate_percent=100.0)
    base = orc.estimate(q).qerror
    cols = tuple(sorted(pair))
    obj = StatObject(table=".climate", columns=cols, capability=mcv,
                     capacity=Capacity(2))
    orc.build_stats([obj], Capacity(2))
    e = orc.estimate(q)
    for s in list(orc.list_stats(".climate")):
        orc.drop_stat(s)
    orc.restore_natural_stats(".climate", estimate_percent=100.0)
    return base, e.qerror


def stage_topk(k=20, oracle_spot=3, target=_REPAIR_TARGET) -> dict:
    queries = _load_queries()
    pg = get_backend("postgres", cfg=_PG)
    orc = get_backend("oracle", cfg=_OR) if oracle_spot > 0 else None

    # rank by PG natural baseline q-error (same ranking as Oracle, corr~1.0)
    scores = []
    pg.restore_natural_stats(".climate")
    for idx, q in enumerate(queries):
        scores.append((pg.estimate(q).qerror, idx))
    # low base => already fine; skip the huge easy majority and also treat
    # 'base<=target' queries as not needing repair.
    highbase = [(err, idx) for err, idx in scores if err and err > target]
    highbase.sort(reverse=True, key=lambda t: t[0])
    top_idx = [idx for _, idx in highbase[:k]]

    rows = []
    for idx in top_idx:
        q = queries[idx]
        base, dom, repaired = _pg_repair(pg, q)
        row = {"qid": q.qid, "truth": q.ground_truth,
               "pg_base": _fmt(base), "dominant_pair": list(dom) if dom else None,
               "pg_repaired": _fmt(repaired),
               "pg_repaired_below_target": bool(repaired is not None
                                                and repaired <= target)}
        if orc is not None and dom is not None and len(rows) < oracle_spot:
            obase, orep = _oracle_verify(orc, q, dom)
            row["oracle_base"] = _fmt(obase)
            row["oracle_dominant_repaired"] = _fmt(orep)
            row["oracle_repaired_below_target"] = bool(orep <= target)
        rows.append(row)

    n_repaired = sum(1 for r in rows if r["pg_repaired_below_target"])
    spots = [r for r in rows if "oracle_dominant_repaired" in r]
    out = {
        "stage": "topk", "k": k, "oracle_spot": oracle_spot,
        "target_qerr": target,
        "summary": {
            "pg_topk_repairable_below_target": n_repaired,
            "pg_total": len(rows),
        },
        "rows": rows,
    }
    if spots:
        out["summary"]["oracle_spot_agree"] = \
            sum(1 for r in spots if r["oracle_repaired_below_target"]
                and r["pg_repaired_below_target"])
        out["summary"]["oracle_spot_checked"] = len(spots)
    return out


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("baseline")
    b.add_argument("--out", default=None)
    t = sub.add_parser("topk")
    t.add_argument("--k", type=int, default=20)
    t.add_argument("--oracle-spot", type=int, default=3)
    t.add_argument("--out", default=None)
    args = ap.parse_args(argv)

    res = {}
    if args.cmd == "baseline":
        res = stage_baseline()
        if args.out:
            Path(args.out).write_text(json.dumps(res, indent=2))
            print("wrote", args.out)
        print("=== baseline distribution ===")
        print("mean", res["stats"]["mean"], "median", res["stats"]["median"],
              "log-pearson", res["stats"]["log_pearson"])
        for thr, v in sorted(res["thresholds"].items(), key=lambda x: float(x[0])):
            print(f"  qerr>={thr}: PG={v['postgres']} Oracle={v['oracle']} "
                  f"jaccard={v['jaccard']}")
    else:  # topk
        res = stage_topk(k=args.k, oracle_spot=args.oracle_spot)
        if args.out:
            Path(args.out).write_text(json.dumps(res, indent=2))
            print("wrote", args.out)
        s = res["summary"]
        print("=== top-k repairability ===")
        print(f"top {len(res['rows'])} high-base queries; PG repairable<="
              f"{res['target_qerr']}: {s['pg_topk_repairable_below_target']}")
        print(f"oracle spot-check {s.get('oracle_spot_checked')}: agree "
              f"{s.get('oracle_spot_agree')}")
        for r in res["rows"][:max(1, len(res["rows"]))]:
            spot = (f" | oracle={r.get('oracle_dominant_repaired')}"
                    if "oracle_dominant_repaired" in r else "")
            print(f"  {r['qid']} truth={r['truth']} base={r['pg_base']} "
                  f"dom={r['dominant_pair']} repaired={r['pg_repaired']}"
                  f"(below? {r['pg_repaired_below_target']}){spot}")
    return 0


if __name__ == "__main__":
    sys.exit(_main())
