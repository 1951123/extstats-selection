"""CLI entry point for the v2 toolkit (thin backend/config-check shell).

NOTE (legacy-cleaning batch-3): this is a *validation shell*, not the full
pipeline CLI.  It models the backend selection & S-grid sampling configuration;
the real S-grid / sample-first experiment drivers live in scratch/
(measure_sgrid.py, etc.).  The interface talks S-grid sampling LEVELS (L0=30k /
L1=300k requested rows), NOT the old v1 three-level "capacity" abstraction.

The CLI wiring is intentionally thin; for now it validates configuration and
backend selection.
"""

from __future__ import annotations

import argparse
import sys

from . import config
from .core.optimize import select_optimizer_class


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="extstats2", description=__doc__)
    p.add_argument("--backend", choices=["postgres", "oracle"], default="postgres")
    p.add_argument("--bench", default="census",
                   choices=["census", "stats_ceb", "stats_ceb_single"],
                   help="benchmark (JOB dropped: join-heavy, extended stats cannot fix join error)")
    p.add_argument("--budget-bytes", type=int, default=0, help="storage budget (0=unlimited)")
    p.add_argument("--maint-budget", type=float, default=None,
                   help="maintenance budget for the ILP (None/0=unconstrained)")
    # S-grid sampling LEVELS (canonical): L0/L1 = requested rows 30k/300k.
    # (The optimizer objective is fixed by cap; worst/p90/geomean are only eval.)
    p.add_argument("--levels", type=int, nargs="+", default=[0, 1],
                   choices=[0, 1],
                   help="S-grid sampling levels to probe (0=30k rows, 1=300k rows)")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("check", help="validate backend selection + S-grid config")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    cfg = config.Config(
        backend=args.backend,
        bench=args.bench,
        budget_bytes=args.budget_bytes,
        maint_budget=args.maint_budget,
        # S-grid sampling level indices to probe (canonical, see SAMPLING_LEVELS)
        sampling_levels=tuple(args.levels),
    )
    if args.command == "check":
        backend = config.get_backend(cfg.backend)
        props = backend.structural_props()
        opt_class = select_optimizer_class(props)
        s_rows = {lv: config.sampling_requested_rows(lv) for lv in args.levels}
        print(f"backend      : {backend.name()}")
        print(f"capabilities : {[str(c) for c in backend.supported_capabilities()]}")
        print(f"bench        : {cfg.bench}")
        print(f"sampling L   : {args.levels} -> requested rows {s_rows} (S-grid)")
        print(f"budget_bytes : {cfg.budget_bytes}")
        print(f"maint_budget : {cfg.maint_budget}")
        print(f"optimizer    : {opt_class}")
        print(f"contract     : sparse={props.sparse_one_stat} "
              f"disjoint={props.disjoint_supported} "
              f"maint={props.maint_structure} cap={props.capacity_model} "
              f"objectives={props.supports_objectives}")
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
