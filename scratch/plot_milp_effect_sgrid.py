"""Plot PG S-grid MILP-effect budget curves for the three benches.

Reads results/milp_effect_sgrid_<bench>_L{0,1}.json for bench in
{census, stats_ceb_single, dmv} and draws one summary figure:

  grid: 3 rows (one per bench) x 2 cols
        col 0 = deployed arithmetic-mean q-error (log y)
        col 1 = deployed geometric-mean q-error (log y)
  each subplot: L0 (blue) and L1 (red) deployed-vs-budget curves + the
  matching constant baseline line (same hue, dashed) read from the corpus rows.
  x = storage budget bytes (log scale).

Output: results/figures/milp_effect_sgrid_3bench.png
Also writes per-bench pngs for convenience.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

ROOT = Path(__file__).resolve().parents[1]
FIGDIR = ROOT / "results" / "figures"
BENCHES = ["census", "stats_ceb_single", "dmv"]
BENCH_LABEL = {"census": "Census", "stats_ceb_single": "Stats-SE (single)", "dmv": "DMV"}
COLOR = {"0": "#1f77b4", "1": "#d62728"}
LV_LABEL = {"0": "L0 (S=30000)", "1": "L1 (S=300000)"}
BUDGETS_DP = [2000, 5000, 10000, 20000, 50000, 100000, 200000, 400000]


def load(bench: str, lv: str) -> dict:
    return json.loads(
        (ROOT / "results" / f"milp_effect_sgrid_{bench}_L{lv}.json").read_text())


def main() -> None:
    FIGDIR.mkdir(parents=True, exist_ok=True)
    data = {}
    for b in BENCHES:
        data[b] = {}
        for lv in ("0", "1"):
            r = json.loads((ROOT / "results"
                            / f"milp_effect_sgrid_{b}_L{lv}.json").read_text())
            d = r["rows"]
            data[b][lv] = {
                "budget": [x["budget_bytes"] for x in d],
                "mean": [x["deployed_mean"] for x in d],
                "geo": [x["deployed_geomean"] for x in d],
                "base_mean": d[0]["baseline_mean"],
                "base_geo": d[0]["baseline_geomean"],
            }

    fig, axes = plt.subplots(3, 2, figsize=(10.5, 10))
    for i, b in enumerate(BENCHES):
        for j, (metric, yname) in enumerate([("mean", "arithmetic mean q-err"),
                                             ("geo", "geometric-mean q-err")]):
            ax = axes[i][j]
            for lv in ("0", "1"):
                d = data[b][lv]
                base = d["base_mean"] if metric == "mean" else d["base_geo"]
                ax.plot(d["budget"], d[metric], "-o", color=COLOR[lv],
                        label=f"{LV_LABEL[lv]} deployed")
                ax.axhline(base, ls="--", lw=1, color=COLOR[lv], alpha=0.6,
                           label=f"{LV_LABEL[lv]} baseline")
            ax.set_xscale("log"); ax.set_yscale("log")
            ax.set_xticks(BUDGETS_DP)
            ax.set_xticklabels([f"{x//1000}k" if x >= 1000 else str(x)
                                for x in BUDGETS_DP], rotation=45, fontsize=8)
            ax.set_xlabel("storage budget (bytes)")
            ax.set_ylabel(yname)
            ax.set_title(f"{BENCH_LABEL[b]} — {yname}")
            ax.grid(True, which="both", alpha=0.3)
            ax.legend(fontsize=7)

    fig.suptitle("PG S-grid MILP effect: deployed q-error vs storage budget (cap=1, one-stat)")
    fig.tight_layout(rect=[0, 0, 1, 0.97])
    out = FIGDIR / "milp_effect_sgrid_3bench.png"
    fig.savefig(out, dpi=150)
    print(f"wrote {out}")

    # optional: per-bench single metric convenience plots not needed
    plt.close(fig)


if __name__ == "__main__":
    main()
