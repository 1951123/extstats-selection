"""Plot maintenance-budget (M, seconds) vs quality for L0 and L1 deployments plus
the argmin-over-level recombined deployment, from results/milp_maint_time.json.

X = deployed-refresh maintenance budget M (seconds)
  - L0 feasible only for M > fixed_L0 = 0.256s ; L1 only for M > fixed_L1 = 2.56s
  - fixed thresholds drawn as vertical gates
Panels: (a) mean, (b) geomean, (c) max per-query, (d) n_selected.
Output: results/figures/milp_maint_time.png
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "results" / "figures" / "milp_maint_time.png"
COLOR = {"0": "#1f77b4", "1": "#d62728", "chosen": "#2ca02c"}
LABEL = {"0": "λ = L0  (fixed 0.256 s)", "1": "λ = L1  (fixed 2.56 s)"}


def main(out: Path) -> None:
    d = json.loads((ROOT / "results" / "milp_maint_time.json").read_text())
    fixed = d["fixed"]
    per = d["per_level"]           # level -> {M_str: {mean,geo,max,n_selected}}
    argmin = d["argmin_over_level"]  # [ {M, choose_level, mean,geo,max,n_selected} ]

    # collect M sorted, and per-level rows present
    Ms = sorted({float(k) for lv in per.values() for k in lv})
    fig, axes = plt.subplots(2, 2, figsize=(11, 8))
    fig.suptitle("Maintenance budget vs. deployed quality (storage fixed at "
                 f"{d['storage_bytes']} B)\n"
                 "dense-11 census; per-λ flat fixed scan + additive per-stat var; "
                 "green = deployment choosing lower-mean λ\n"
                 "solid black line = q-error floor 1 (=10^0, perfect estimate); "
                 "log-y anchored at bottom=1 (quality panels only)", fontsize=9.5)

    panels = [
        (axes[0][0], "mean", "deployed mean q-error", True),
        (axes[0][1], "geo", "geometric-mean q-error", True),
        (axes[1][0], "max", "max per-query q-error (tail)", True),
        (axes[1][1], "n_selected", "# statistics deployed", False),
    ]
    # vertical feasibility gates
    for axm, _, _, _ in panels:
        for lv, col in (("0", COLOR["0"]), ("1", COLOR["1"])):
            axm.axvline(float(fixed[lv]), color=col, ls=":", lw=1.2, alpha=0.7)
        axm.set_xlabel("maintenance budget M (s)")
        axm.grid(True, which="both", alpha=0.25)

    # per-level curves
    for lv in ("0", "1"):
        rows = per[lv]
        xs = sorted(float(k) for k in rows)
        for axm, key, _, logy in panels:
            ys = [rows[str(x)][key] for x in xs]
            if logy:
                axm.semilogy(xs, ys, marker="o", color=COLOR[lv], lw=1.7,
                             label=LABEL[lv], alpha=0.85)
            else:
                axm.plot(xs, ys, marker="o", color=COLOR[lv], lw=1.7,
                         label=LABEL[lv], alpha=0.85)
    # argmin-over-level (green), connected by M
    xs = [b["M"] for b in argmin]
    for axm, key, _lbl, logy in panels:
        ys = [b[key] for b in argmin]
        if logy:
            axm.semilogy(xs, ys, marker="D", color=COLOR["chosen"], lw=1.9,
                         ls="--", label="chosen λ (lower-mean)")
        else:
            axm.plot(xs, ys, marker="D", color=COLOR["chosen"], lw=1.9,
                     ls="--", label="chosen λ (lower-mean)")

    # -- anchor the quality (log-y) panels' lower bound at q-error = 1 (=10^0)
    for axq in (axes[0][0], axes[0][1], axes[1][0]):
        axq.set_ylim(bottom=1.0)
        axq.axhline(1.0, color="black", ls="-", lw=0.8, alpha=0.35)

    axes[0][0].set_ylabel("mean q-error")
    axes[0][1].set_ylabel("geometric-mean q-error")
    axes[1][0].set_ylabel("max per-query q-error")
    axes[1][1].set_ylabel("# statistics")
    axes[0][0].set_title("mean q-error")
    axes[0][1].set_title("geometric-mean q-error")
    axes[1][0].set_title("max per-query q-error (tail)")
    axes[1][1].set_title("# statistics deployed")
    axes[0][0].legend(fontsize=7)
    axes[1][1].legend(fontsize=7)

    fig.tight_layout(rect=(0, 0, 1, 0.90))
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=150)
    print(f"wrote {out}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(OUT))
    a = ap.parse_args()
    main(Path(a.out))
