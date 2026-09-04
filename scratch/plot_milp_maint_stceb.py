"""Plot maintenance-budget (M, seconds) vs quality for stats_CEB_single across
λ levels 0/1/2 plus the argmin-over-level recombined deployment, with the
per-table activation story.

Reads results/milp_maint_time_stceb_single_L{0,1,2}.json (from
scratch/measure_milp_maint_stceb.py). The maintenance model is per-active-table:
to serve a table's queries you must refresh that table once (fixed F_t(λ)),
so activating more tables costs more budget. Green line = for budget M choose
the lowest-mean λ deployment.

Panels: (a) mean, (b) geomean, (c) #tables activated, (d) #statistics deployed.
Output: results/figures/milp_maint_time_stceb_single.png
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

ROOT = Path(__file__).resolve().parents[1]
FIG = ROOT / "results" / "figures" / "milp_maint_time_stceb_single.png"
LEVELS = ("0", "1", "2")
COLOR = {"0": "#1f77b4", "1": "#d62728", "2": "#ff7f0e", "chosen": "#2ca02c"}
LABEL = {"0": "λ = L0", "1": "λ = L1", "2": "λ = L2"}


def load(lv: str):
    return json.loads((ROOT / "results" / f"milp_maint_time_stceb_single_L{lv}.json").read_text())


def main(out: Path) -> None:
    ds = {lv: load(lv) for lv in LEVELS}
    Ms = sorted({float(r["M"]) for lv in LEVELS for r in ds[lv]["rows"]})

    fig, axes = plt.subplots(2, 2, figsize=(11, 8))
    fig.suptitle(
        "stats_CEB_single: maintenance budget vs. deployed quality "
        f"(storage fixed at {ds['1']['storage_bytes']} B)\n"
        "multi-table per-active-table fixed ANALYZE + additive per-stat var; "
        "green = choose lowest-mean λ per budget M",
        fontsize=9.5)
    panels = [
        (axes[0][0], "mean", "deployed mean q-error", True),
        (axes[0][1], "n_tables_active", "# tables activated", False),
        (axes[1][0], "n_selected", "# statistics deployed", False),
        (axes[1][1], "var_used", "per-stat var used (s)", False),
    ]
    for axm, _, _, _ in panels:
        axm.set_xlabel("maintenance budget M (s)")
        axm.grid(True, which="both", alpha=0.25)

    for lv in LEVELS:
        d = ds[lv]
        rows = sorted(d["rows"], key=lambda r: r["M"])
        for axm, key, _, _ in panels:
            axm.plot([r["M"] for r in rows], [r[key] for r in rows],
                     color=COLOR[lv], marker="o", ms=3, label=LABEL[lv])
            axm.axhline(float(d["baseline_mean"]), color=COLOR[lv], ls=":", lw=1.0, alpha=0.6)
    # argmin over levels
    chosen_m = []
    for M in Ms:
        cand = [(lv, next(r for r in ds[lv]["rows"] if abs(r["M"] - M) < 1e-9))
                for lv in LEVELS]
        lv, r = min(cand, key=lambda x: x[1]["mean"])
        chosen_m.append((M, r))
    for axm, key, _, _ in panels:
        axm.plot([x for x, _ in chosen_m], [r[key] for x, r in chosen_m],
                 color=COLOR["chosen"], marker="x", lw=2, label="argmin over λ")

    for axm, key, ylab, logy in panels:
        axm.set_ylabel(ylab)
        axm.legend(fontsize=8)
        if logy:
            axm.set_yscale("log")
            axm.set_ylim(bottom=max(min(axm.get_ylim()[1] * 1e-3, 0.9), 0.85))
    # annotate the dominant table thresholds on first panel
    ax = axes[0][0]
    fixed = {t: ds["1"]["fixed_per_table_L"][t] for t in ds["1"]["fixed_per_table_L"]}
    for t in (".postlinks", ".users", ".posts", ".posthistory"):
        ax.axvline(fixed[t], color="grey", ls=":", lw=1, alpha=0.7)
        ax.text(fixed[t], ax.get_ylim()[1] * 0.98, f"{t[1:]}~{fixed[t]:.2f}s",
                rotation=90, fontsize=7, va="top", alpha=0.7)
    fig.tight_layout(rect=(0, 0, 1, 0.94))
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=140)
    print(f"wrote {out}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(FIG))
    a = ap.parse_args()
    main(Path(a.out))
