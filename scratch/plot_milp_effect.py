"""Plot budget-vs-quality curves for the MILP effect-time at λ=0 and λ=1.

Reads results/milp_effect_time[_{bench}]_L{0,1}.json (top-level {"level":..,
"rows":[...]}) and produces a 2x2 figure:
  (a) mean per-query q-error
  (b) geometric-mean q-error
  (c) max per-query q-error (tail)
  (d) MILP solve time (s)
x-axis = storage budget in bytes (log scale), one curve per λ tier.
Output: results/figures/milp_effect_time[_<bench>].png.

Usage:
  .venv/bin/python -u scratch/plot_milp_effect.py                    # census (default)
  .venv/bin/python -u scratch/plot_milp_effect.py --bench dmv        # dmv
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

ROOT = Path(__file__).resolve().parents[1]

# shared style
COLOR = {"0": "#1f77b4", "1": "#d62728"}
LABEL = {"0": "λ = L0  (S/300 = 100)", "1": "λ = L1  (S/300 = 1000)"}


def load_lv(bench: str, lv: str) -> dict:
    stem = f"milp_effect_time{'_' + bench if bench != 'census' else ''}_L{lv}.json"
    p = ROOT / "results" / stem
    return json.loads(p.read_text())


def baseline_metrics(bench: str) -> dict:
    """Exact per-level baseline (no extended stats) mean/geo/max from the corpus."""
    import glob
    pat = (ROOT / "results" / "per_lambda" / bench / "postgres" / "*.json")
    files = sorted(glob.glob(str(pat)))
    vals = {"0": [], "1": []}
    for f in files:
        if Path(f).name == "_meta.json":
            continue
        d = json.loads(Path(f).read_text())
        for lk in ("0", "1"):
            L = d.get("by_lambda", {}).get(lk)
            if L is not None:
                q = L["baseline"]["qerror"]
                if q == q:
                    vals[lk].append(q)
    out = {}
    for lk, x in vals.items():
        n = len(x)
        out[lk] = {
            "mean": sum(x) / n,
            "geo": math.exp(sum(math.log(max(v, 1e-12)) for v in x) / n),
            "max": max(x),
        }
    return out


def main(bench: str, out: Path) -> None:
    data = {lv: load_lv(bench, lv) for lv in ("0", "1")}
    fig, axes = plt.subplots(2, 2, figsize=(11, 8))

    # Baseline (no extended stats) reference metric per level, computed from corpus.
    baseline = baseline_metrics(bench)  # {lv: {"mean","geo","max"}}
    fig.suptitle(
        f"Phase-2 MILP: storage budget vs. quality & solve time  ({bench})\n"
        "only skip_worse_than_baseline pruning\n"
        "dashed lines = no-ext single-column baseline per λ; L0/L1 baselines "
        f"(mean {baseline['0']['mean']:.2f}/{baseline['1']['mean']:.2f}, "
        f"geo {baseline['0']['geo']:.2f}/{baseline['1']['geo']:.2f}, "
        f"max {baseline['0']['max']:.0f}/{baseline['1']['max']:.0f})\n"
        "solid black line = q-error floor 1 (=10^0, perfect estimate); log-y anchored at bottom=1",
        fontsize=10,
    )

    panels = [
        # (ax, key, title, metric-in-baseline)
        (axes[0][0], "deployed_mean", "deployed mean q-error", "mean"),
        (axes[0][1], "deployed_geomean", "geometric-mean q-error", "geo"),
        (axes[1][0], "max_per_query_after", "max per-query q-error (tail)", "max"),
    ]

    for ax, key, title, bmet in panels:
        for lv in ("0", "1"):
            rows = data[lv]["rows"]
            B = [r["budget_bytes"] for r in rows]
            y = [r[key] for r in rows]
            ax.semilogy(B, y, marker="o", color=COLOR[lv], label=LABEL[lv], lw=1.8)
            # baseline reference: horizontal dashed line, color-matched
            # (L0 and L1 no-ext single-column baselines are nearly identical,
            #  so the two dashed lines visually coincide -- noted in the caption.)
            ax.axhline(baseline[lv][bmet], color=COLOR[lv], ls="--", lw=1.2, alpha=0.6)
        ax.set_xscale("log")
        ax.set_xlabel("storage budget (bytes)")
        ax.set_ylabel("q-error (log)")
        ax.set_title(title, fontsize=10)
        ax.grid(True, which="both", alpha=0.25)
        # anchor the y-lower bound at q-error = 1 (= 10^0), the semantic floor
        # (q-error >= 1; 1 = perfect estimate). Curves approach but never cross it.
        ax.set_ylim(bottom=1.0)
        ax.axhline(1.0, color="black", ls="-", lw=0.8, alpha=0.35)

    # legend on mean panel: keep both data series + one dashed line repr
    handles = []
    for lv in ("0", "1"):
        handles.append(plt.Line2D([0], [0], marker="o", color=COLOR[lv], lw=1.8,
                                  label=LABEL[lv]))
    handles.append(plt.Line2D([0], [0], color="#555555", ls="--", lw=1.2,
                              label="no-ext baseline (L0≈L1)"))
    axes[0][0].legend(handles=handles, fontsize=8, loc="center left")

    # panel (d): solve time
    ax = axes[1][1]
    for lv in ("0", "1"):
        rows = data[lv]["rows"]
        B = [r["budget_bytes"] for r in rows]
        t = [r["solve_seconds"] for r in rows]
        ax.plot(B, t, marker="o", color=COLOR[lv], label=LABEL[lv], lw=1.8)
    ax.set_xscale("log")
    ax.set_xlabel("storage budget (bytes)")
    ax.set_ylabel("solve time (s)")
    ax.set_title("MILP solve time", fontsize=10)
    ax.grid(True, which="both", alpha=0.25)
    ax.legend(fontsize=8)

    fig.tight_layout(rect=(0, 0, 1, 0.90))
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=150)
    print(f"wrote {out}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--bench", default="census",
                    help="workload basename; also selects data file prefix and "
                         "per_lambda corpus for baseline refs (census|dmv)")
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    default_out = (ROOT / "results" / "figures" /
                   (f"milp_effect_time_{a.bench}.png"
                    if a.bench != "census" else "milp_effect_time.png"))
    out = Path(a.out) if a.out else default_out
    main(a.bench, out)
