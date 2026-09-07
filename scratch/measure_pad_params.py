"""Incremental "pad-only" representation-param measurement (scheme A).

Context / principle (2026-09-03)
--------------------------------
We already paid the cost to measure higher representation params (p25/50/100/
[1000/10000]) on the PG census corpus. When we switch the census decision grid to
[10,25,50] we should NOT re-measure / discard the higher-param columns: the cost is
"sunk" and the higher-param readings remain valid in the corpus (the optimizer /
decision layer may choose to ignore them). Instead we PAD-ONLY: add any missing
*lower* grid points (here p=10) into each (colset, lambda) cell, preserving every
existing cell bit-for-bit. Idempotent: re-running never re-measures what is present.

This is the generic mechanism for *any* future param-grid / lambda-tier change:
- compute the set of cells already present per (qid, lambda, colset);
- measure only cells the *new grid* requires that are absent;
- merge them into the per-query JSON, never overwriting present cells.

Protocol-M is used when the backend supports catalog-mask (build missing objects
in ONE shared ANALYZE per (query, lambda), read each by masking); else Protocol-A.

Same-sample caveat / honesty marker
-----------------------------------
Each pad re-enters the (query, lambda) lambda-state and draws a NEW ANALYZE
sample before building the missing objects. So a padded p10 row is measured on a
*different sampling draw* than the slot's original baseline / p25/50/100 rows
(which were built under the slot's original draw). ANALYZE is stochastic, so a
true full re-measure would not reproduce itself either; padded rows are valid
candidate points but their qerror is NOT same-sample-comparable to the slot's
other params at fine precision. To make this traceable every padded row carries
``"pad": true`` so consumers can distinguish cross-sample padded readings from
the same-sample original ones (and, if needed, treat low-fidelity rows more
conservatively).

Usage (PG census)::
    python scratch/measure_pad_params.py postgres \
        --out results/measure/census/postgres --table .climate --add-params 10 100
Writes: back-merged <qid>.json with absent params appended (tagged ``pad:true``),
and recomputes _meta.param_tiers to reflect the actual coverage grid.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from extstats2.backend.base import StatObject
from extstats2.backend.capabilities import Capacity
from extstats2.config import DBConfig, get_backend
from extstats2.core.measure_io import read_meta, write_meta


def existing_cells(file_block: dict):
    """Return {(lambda_str, frozenset(cols)): set(params)} already present."""
    out = {}
    for lv, slot in file_block.get("by_lambda", {}).items():
        for c in slot.get("candidates", []):
            out.setdefault((lv, frozenset(c["cols"])), set()).add(c["param"])
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("backend", choices=("postgres", "oracle"))
    ap.add_argument("--out", required=True)
    ap.add_argument("--table", default=".climate")
    ap.add_argument("--add-params", type=int, nargs="+", default=[10])
    ap.add_argument("--bench", default="census")
    args = ap.parse_args()

    table = args.table
    if args.backend == "postgres":
        cfg = DBConfig(host="localhost", port=5432, user="postgres",
                       password="postgres", dbname="census")
    else:
        cfg = DBConfig(host="localhost", port=1521, user="SYSTEM",
                       password="lxf82073077", service="FREEPDB1")
    be = get_backend(args.backend, cfg=cfg)
    mcv = next(c for c in be.supported_capabilities()
               if c.supported and c.name == "mcv")
    use_pm = be.supports_catalog_mask()

    outdir = Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)
    qids = sorted(p.stem for p in outdir.glob("query.*.json"))
    pad = tuple(sorted(args.add_params))

    from extstats2.bench import load_benchmark
    Q = {q.qid: q for q in load_benchmark(args.bench)}

    total_new = 0
    changed_any = False
    for qid in qids:
        pf = outdir / f"{qid}.json"
        block = json.loads(pf.read_text())
        query = Q.get(qid)
        if query is None:
            print(f"[skip] {qid}: not in benchmark ({args.bench})")
            continue
        present = existing_cells(block)
        # cells that need new params: (lv, colset) -> missing [params]
        cells = {}
        for (lv, cols), ps in present.items():
            miss = [pp for pp in pad if pp not in ps]
            if miss:
                cells[(lv, cols)] = miss
        if not cells:
            continue

        touched = sorted({lv for (lv, _) in cells})
        changed = False
        for lv in touched:
            want = [(cols, pp) for (l, cols), ps in cells.items()
                    if l == lv for pp in ps]
            if not want:
                continue
            # enter lambda state once for this query+lambda; baseline is kept
            be.enter_sampling_state(table, int(lv))
            for s in list(be.list_stats(table)):
                be.drop_stat(s)

            objs = []  # (cols, StatObject, param)
            for cols, pp in want:
                cols_l = "_".join(str(c).lower() for c in sorted(cols))
                name = (f"pad_{table.rpartition('.')[2]}_{cols_l}_l{lv}p{pp}")
                o = StatObject(table=table, columns=tuple(sorted(cols)),
                               capability=mcv,
                               capacity=Capacity(int(lv), label=f"L{lv}"),
                               name=name)
                be.create_stat(o)
                objs.append((cols, o, pp))
            try:
                if use_pm:
                    be.build_stat_params_batch([(o, pp) for _, o, pp in objs])
                    driver = be.catalog_driver()
                    all_objs = [o for _, o, _ in objs]
                    bkp = driver.backup_payloads(all_objs)
                    rows = be.sample_rows_at_level(table, int(lv))
                    n = be.num_rows(table) or 1.0
                    for cols, o, pp in objs:
                        bkp.mask_all_but({o})
                        est = be.estimate(query)
                        size = be.stat_size_bytes(o)
                        mv = be.stat_maintain_var(o)
                        bkp.restore()
                        lam_q = ((query.ground_truth / n) * (rows or 0.0)
                                 if query.ground_truth else None)
                        block["by_lambda"][lv]["candidates"].append({
                            "cols": list(cols), "param": pp,
                            "estimate": est.estimate,
                            "qerror": (est.qerror if est.qerror is not None
                                       else float("nan")),
                            "lambda_q": lam_q, "size_bytes": size,
                            "maint_var": mv, "pad": True})
                        total_new += 1
                    changed = True
                    try:
                        bkp.close()
                    except Exception:
                        pass
                else:
                    for cols, o, pp in objs:
                        be.build_stat_param(o, pp)
                        est = be.estimate(query)
                        size = be.stat_size_bytes(o)
                        mv = be.stat_maintain_var(o)
                        block["by_lambda"][lv]["candidates"].append({
                            "cols": list(cols), "param": pp,
                            "estimate": est.estimate,
                            "qerror": (est.qerror if est.qerror is not None
                                       else float("nan")),
                            "lambda_q": None, "size_bytes": size,
                            "maint_var": mv,
                            "pad": True})
                        total_new += 1
                    changed = True
            finally:
                for _, o, _ in objs:
                    try:
                        be.drop_stat(o)
                    except Exception:
                        pass
        if changed:
            tmp = outdir / f"{qid}.json.tmp"
            tmp.write_text(json.dumps(block))
            tmp.replace(pf)
            changed_any = True
            print(f"[pad] {qid}: +{len(cells)} cells updated", flush=True)

    for s in list(be.list_stats(table)):
        be.drop_stat(s)

    # --- keep _meta.json honest about the now-effective coverage grid. -------
    # _meta.param_tiers is meant to state which representation params this
    # workload has actually measured. After padding new points (or on any idempotent
    # re-run) it should equal the union over every present candidate cell, so we
    # recompute it from the corpus rather than just unioning --add-params (which
    # may not have applied everywhere, e.g. a lattice cap). Optimization consumers
    # read the per-file candidate rows directly, so this is metadata/coverage
    # correctness rather than a functional gate.
    meta = read_meta(outdir)
    if meta is not None:
        present_any = set()
        for qid in qids:
            try:
                blk = json.loads((outdir / f"{qid}.json").read_text())
            except Exception:
                continue
            for slot in blk.get("by_lambda", {}).values():
                for c in slot.get("candidates", []):
                    present_any.add(int(c["param"]))
        if present_any:
            # preserve declared order (original grid) but include all present points
            declared = list(meta.param_tiers)
            merged = list(dict.fromkeys(declared + sorted(present_any)))
            if tuple(merged) != tuple(meta.param_tiers):
                meta.param_tiers = tuple(merged)
                write_meta(outdir, meta)
                print(f"[meta] _meta.json param_tiers -> {list(merged)}")

    print(f"[run] DONE: padded {total_new} new cells across {len(qids)} files"
          f" (changed={changed_any})")


if __name__ == "__main__":
    main()
