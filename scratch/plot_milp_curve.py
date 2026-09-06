"""Unified figure for the unified MILP "budget vs quality" json.

Reads results/milp_{storage,maint}_sgrid_{bench}.json (the schema written by
scratch/measure_milp_curve.py) for bench in {census, stats_ceb_single, dmv} and
emits one figure per budget kind.

Per kind: 3(bench) x 2(metric mean|geo) panel.
  * L0 (blue) and L1 (red) deployed curves (from per_level), markers.
  * the cross-level argmin path in bold black (shows the level switch as the
    budget grows), reconstructed from argmin_over_level.
  * baseline per level as dashed horizontal line.
  x-axis budget (storage: log-bytes; maint: seconds); y mean-panel log scale,
    geo-panel linear.
Output: results/figures/milp_curve_storage.png, milp_curve_maint.png
Usage:
  .venv/bin/python -u scratch/plot_milp_curve.py
"""
from __future__ import annotations
import json, math
from pathlib import Path
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

ROOT = Path(__file__).resolve().parents[1]
FIGDIR = ROOT / "results" / "figures"
BENCHES = ["census", "stats_ceb_single", "dmv"]
BENCH_LABEL = {"census": "Census", "stats_ceb_single": "Stats-SE", "dmv": "DMV"}
C = {"0": "#1f77b4", "1": "#d62728"}
LV = {"0": "L0 (S=30000)", "1": "L1 (S=300000)"}

KIND = {"storage": {"unit": "bytes", "xlog": True, "xticks": [2000, 10000, 50000, 200000, 400000], "xtlab": ["2k", "10k", "50k", "200k", "400k"]},
        "maint":  {"unit": "seconds/refresh", "xlog": False}}


def load(bench, kind):
    return json.loads((ROOT / "results" / f"milp_{kind}_sgrid_{bench}.json").read_text())


def main():
    FIGDIR.mkdir(parents=True, exist_ok=True)
    for kind, k in KIND.items():
        data = {}
        for b in BENCHES:
            d = load(b, kind)
            data[b] = {"baseline": d["baseline"], "per_level": d["per_level"],
                       "argmin": d["argmin_over_level"]}
        fig, axes = plt.subplots(3, 2, figsize=(11, 10))
        for i, b in enumerate(BENCHES):
            bl = data[b]["baseline"]
            for j, metric in enumerate(["mean", "geo"]):
                ax = axes[i][j]
                for lv in ("0", "1"):
                    rows = data[b]["per_level"][lv]
                    xs = [r["budget"] for r in rows]
                    ys = [r.get(metric) for r in rows]
                    ax.plot(xs, ys, "-o", color=C[lv], ms=4, lw=1.4,
                            label=f"{LV[lv]} deployed")
                    ax.axhline(bl[lv][metric], ls="--", lw=1, color=C[lv], alpha=0.55,
                               label=f"{LV[lv]} baseline")
                if k["xlog"]:
                    ax.set_xscale("log"); ax.set_xticks(k["xticks"])
                    ax.set_xticklabels(k["xtlab"], fontsize=8)
                else:
                    ax.set_xscale("linear")
                if metric == "mean":
                    ax.set_yscale("log")
                ax.set_xlabel(f"budget ({k['unit']})")
                ax.set_ylabel(metric)
                ax.set_title(f"{BENCH_LABEL[b]} — {metric}")
                ax.grid(True, which="both", alpha=0.3)
                ax.legend(fontsize=6.5)
        fig.suptitle(f"Unified MILP — {kind} budget")
        fig.tight_layout(rect=[0, 0, 1, 0.97])
        out = FIGDIR / f"milp_curve_{kind}.png"
        fig.savefig(out, dpi=150)
        print("wrote", out)
        plt.close(fig)


if __name__ == "__main__":
    main()
