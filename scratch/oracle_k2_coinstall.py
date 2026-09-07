"""Oracle + Census: k=1 vs k=2 one-stat-sufficiency CO-INSTALLATION test.

Purpose
-------
v2 uses Oracle only to *assist* (not replicate PG-scale) in validating one-stat
sufficiency engine-independently. The decisive form of that claim is: a query's
q-error is driven by ONE dominant (correlation cluster) stat, so adding a second,
genuinely-useful, non-overlapping stat gives ~no marginal gain (keep{A} ≈
keep{A,B}).

PG census data (v1 results/phase1_census_mcv_6level.json + the per-λ corpus)
shows census is overwhelmingly single-cluster: at arity-2, essentially no worst
query has a second *useful* non-overlap — EXCEPT **query.274**, where

    A = (dRearning, dWeek89) -> qerr ~1.1   (dominant; base ~108)
    B = (dHour89,  iMeans)   -> qerr ~38    (genuinely useful 2nd, disjoint)

So query.274 is the flagship non-vacuous k=2 case: does Oracle's column-group,
when A and B are CO-INSTALLED (keep{A,B}), give any gain over keep{A} alone?
The same flow runs the 3 existing mini queries (184/62/61) as single-dominant
controls (there A alone suffices and their 2nd non-overlap is ~useless).

What this reports (per query, per λ level 0/1):
  - base        : no-ext per-λ baseline q-error (natural single-col stats)
  - keep_A      : only the dominant pair A live
  - keep_B      : only the useful 2nd pair B live   (Q274; else ~base)
  - keep_AB     : A AND B co-installed (the k=2 marginal test)

Interpretation: one-stat sufficiency on Oracle predicts keep_AB ~ keep_A
(adding the disjoint, individually-useful B does not further repair A's
dominant-cluster error).

Usage: python scratch/oracle_k2_coinstall.py   (Oracle FREEPDB1, CLIMATE)
Writes: results/oracle_k2_coinstall.json
"""
from __future__ import annotations

import json
from pathlib import Path

from extstats2.backend.oracle import OracleBackend
from extstats2.backend.capabilities import Capability, SamplingLevel
from extstats2.backend.base import StatObject
from extstats2.bench import load_benchmark
from extstats2.config import DBConfig

# The (query -> candidate isolation study) table. For each query we name the
# "dominant" pair A and the "useful 2nd" pair B (columns disjoint from A). A
# candidate pair with B=None means "no useful 2nd" (control).
STUDY = {
    # flagship: a genuinely useful non-overlapping 2nd EXISTS (hard k2 test)
    "query.274": {
        "A": ["dRearning", "dWeek89"],
        "B": ["dHour89", "iMeans"],
    },
    # single-dominant controls (census_mini): A repairs, no useful 2nd
    "query.184": {
        "A": ["iDisabl1", "iRspouse"],
        "B": None,  # 2nd non-overlap (~dDepart,dRpincome) stays ~baseline
    },
    "query.62": {
        "A": ["iRspouse", "iWork89"],
        "B": None,
    },
    "query.61": {
        "A": ["iDisabl2", "iYearsch"],
        "B": None,
    },
}

LEVELS = (0, 1, 2)            # default all; L2 (full-scan) is the fidelity-solid
                              # regime that matches v1 PG target=10000. Pass a
                              # subset as argv (e.g. "2") to run only L2.


def _cap(mcv: Capability) -> Capability:
    return mcv


def main() -> None:
    import sys
    levels = tuple(int(x) for x in sys.argv[1:]) or LEVELS
    cfg = DBConfig(host="localhost", port=1521, user="SYSTEM",
                   password="lxf82073077", service="FREEPDB1")
    be = OracleBackend(cfg=cfg)
    mcv = next(c for c in be.supported_capabilities() if c.name == "mcv")

    Q = {q.qid: q for q in load_benchmark("census")}
    ncols = be.num_rows(".climate")

    # ensure clean baseline before we start
    for s in list(be.list_stats(".climate")):
        be.drop_stat(s)

    out = {"bench": "census", "backend": "oracle", "table": "CLIMATE",
           "study": {}, "levels": list(levels)}
    for qid, spec in STUDY.items():
        q = Q[qid]
        rows = []
        AP = tuple(spec["A"])
        BP = tuple(spec["B"]) if spec["B"] else None
        for lvl in levels:
            # natural no-ext per-λ baseline
            be.enter_sampling_state(".climate", lvl)
            for s in list(be.list_stats(".climate")):
                be.drop_stat(s)
            be_base = be.estimate(q)
            base_qerr = be_base.qerror

            def _obj(cols):
                return StatObject(table=".climate", columns=cols,
                                  capability=mcv,
                                  sampling_level=SamplingLevel(lvl, label=f"L{lvl}"),
                                  name="k2_" + "_".join(cols))

            def _measure_keep(keep):
                # hold only ``keep`` column-groups live, measure; isolate restores.
                with be.isolate({_obj(c) for c in keep}, ".climate"):
                    est = be.estimate(q)
                return est

            oA = _measure_keep([AP])
            oAB = _measure_keep([AP] + ([BP] if BP else []))  # k=2 co-install
            oB = _measure_keep([BP]) if BP else None

            row = {
                "level": lvl,
                "S_rows": be.sample_rows_at_level(".climate", lvl),
                "base": {"estimate": be_base.estimate, "qerror": base_qerr},
                "keep_A": {"cols": AP, "estimate": oA.estimate,
                           "qerror": oA.qerror},
                "keep_AB": {"cols_spec": AP, "n_kept": 2 if BP else 1,
                            "estimate": oAB.estimate, "qerror": oAB.qerror},
            }
            if BP:
                row["keep_B"] = {"cols": BP, "estimate": oB.estimate,
                                 "qerror": oB.qerror}
            rows.append(row)
            print(f"[{qid}] L{lvl}: base={base_qerr:.2f}  "
                  f"keep_A={oA.qerror:.3f}  keep_AB={oAB.qerror:.3f}"
                  + (f"  keep_B={oB.qerror:.2f}" if BP else ""), flush=True)
        out["study"][qid] = {"actual": q.ground_truth, "rows": rows}

    for s in list(be.list_stats(".climate")):
        be.drop_stat(s)
    dest = Path("results")
    dest.mkdir(exist_ok=True)
    p = dest / "oracle_k2_coinstall.json"
    p.write_text(json.dumps(out, indent=2))
    print(f"[run] DONE -> {p}")


if __name__ == "__main__":
    main()
