"""Oracle + Census: engine-independent single-group-suffices screen.

Purpose / context (2026-09-03)
-------------------------------
We established that the "hard" k=1-vs-k=2 case (a genuinely useful non-overlapping
2nd extstat) does NOT exist in census (single-cluster bad tail) or stats_CEB_single
(mild base, one stat reaches ~1). Oracle's column-group also does not engage for
the wide-range-dominated multi-cluster queries (query.274: estimate unchanged even
at full scan, §6.3d2).

So the Oracle-assisted validation of one-stat sufficiency converges to the WEAKER
but engine-independent claim: *for an equality/(low-range) correlated census bad
tail query, the single PG-dominant column-group is also detected as dominant on
Oracle and alone repairs the query to ~baseline-good.* This screen verifies that
on a shortlist of equality-ish worst queries, cheaply at L1 (per group ~2 s) and
spot-checks determinism at L2 for any that engage+repair.

What it reports (per query): base qerr + keep{dominant} qerr, and whether Oracle's
estimate CHANGED at all (i.e. the CBO engaged the group — NOT a query.274-style
non-application).

Shortlist columns are the PG L1-dominant repair pairs (from the per-lambda corpus).
Excludes query.274 (range-dominated -> Oracle ignores col-groups there).

Usage: python scratch/oracle_single_group_screen.py [levels...]
Writes: results/oracle_single_group_screen.json
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from extstats2.backend.base import StatObject
from extstats2.backend.capabilities import Capacity
from extstats2.bench import load_benchmark
from extstats2.config import DBConfig
from extstats2.backend.oracle import OracleBackend

# (qid -> PG-dominant arity-2 repair pair). Seed = PG L1 per-lambda corpus best
# per query; the mini trio (184/62/61) reconfirmed (already in census_mini/oracle).
SHORTLIST = {
    "query.184": ["iDisabl1", "iRspouse"],
    "query.62":  ["iRspouse", "iWork89"],
    "query.61":  ["iDisabl2", "iYearsch"],
    "query.295": ["iMeans", "iWorklwk"],
    "query.201": ["dDepart", "dHours"],
    "query.123": ["dIncome1", "iWork89"],
    "query.72":  ["iRiders", "iWork89"],
    "query.161": ["dRpincome", "iRlabor"],
    "query.104": ["dRearning", "iWorklwk"],
    "query.221": ["dRearning", "dWeek89"],
}

LEVELS = (1, 2)   # L1 primary (fast ~2s/gather); L2 full-scan determinism spot
DEFAULT_LEVELS = (1,)


def main() -> None:
    levels = tuple(int(x) for x in sys.argv[1:]) or DEFAULT_LEVELS
    cfg = DBConfig(host="localhost", port=1521, user="SYSTEM",
                   password="lxf82073077", service="FREEPDB1")
    be = OracleBackend(cfg=cfg)
    mcv = next(c for c in be.supported_capabilities() if c.name == "mcv")
    Q = {q.qid: q for q in load_benchmark("census")}
    for s in list(be.list_stats(".climate")):
        be.drop_stat(s)

    out = {"bench": "census", "backend": "oracle", "levels": list(levels),
           "study": {}}
    for qid, dom in SHORTLIST.items():
        q = Q[qid]
        rows = []
        dom = tuple(dom)
        for lvl in levels:
            be.enter_lambda_state(".climate", lvl)
            for s in list(be.list_stats(".climate")):
                be.drop_stat(s)
            be_base = be.estimate(q)
            obj = StatObject(table=".climate", columns=dom, capability=mcv,
                             capacity=Capacity(lvl, label=f"L{lvl}"),
                             name="sgs_" + "_".join(dom))
            with be.isolate({obj}, ".climate"):
                e = be.estimate(q)
            est_changed = (e.estimate != be_base.estimate)
            rows.append({
                "level": lvl,
                "base": {"estimate": be_base.estimate,
                         "qerror": be_base.qerror},
                "keep_dominant": {"cols": list(dom),
                                  "estimate": e.estimate,
                                  "qerror": e.qerror},
                "estimate_changed": est_changed,  # False => col-group NOT applied
            })
            flag = "CHANGED/engages" if est_changed else "NOT-ENGAGED"
            print(f"[{qid}] L{lvl}: base={be_base.qerror:.2f} "
                  f"keep{list(dom)}->{e.qerror if e.qerror is not None else None:.2f} "
                  f"({flag})", flush=True)
        out["study"][qid] = {"actual": q.ground_truth, "dominant": list(dom),
                             "rows": rows}

    for s in list(be.list_stats(".climate")):
        be.drop_stat(s)
    p = Path("results") / "oracle_single_group_screen.json"
    p.write_text(json.dumps(out, indent=2))
    print(f"[run] DONE -> {p}")


if __name__ == "__main__":
    main()
