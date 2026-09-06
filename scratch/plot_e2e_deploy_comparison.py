"""Grouped bar chart: TRUE deployed q-error vs per-metric predicted, 4 strategies.

census climate, S-grid / L1 (S=300k), 100 KB budget, real EXPLAIN deploy per scheme.

Semantics (S-grid, see docs/e2e-deployment-interference-results.md):
- naive / topo / FB share the same interference-free predicted set (MILP L1 @100 KB:
  mean 1.409, geo 1.213, max 14.19), i.e. the "as if each query is served alone" optimum.
- disjoint (formerly "Option-A") predicts its OWN (worse) quality because column-mutual
  exclusion starves the workload.
Each panel shows, for that metric, predicted vs TRUE side by side.
Panels: mean / geometric-mean / max (log-y for max).
Result files (present in results/, all *_sgrid_L1_100KB.json):
  naive    e2e_sgrid_naive_L1_100KB.json           (predicted_metrics + true_metrics)
  topo     e2e_true_ordered_topo_sgrid_L1_100KB.json (true_metrics + scalar predicted_mean)
  FB       e2e_true_ordered_fb_sgrid_L1_100KB.json   (true_metrics + scalar predicted_mean)
  disjoint e2e_sgrid_disjoint_L1_100KB.json         (predicted_metrics + true_metrics)
"""
from __future__ import annotations
import json
from pathlib import Path
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "results" / "figures" / "e2e_deploy_comparison_sgrid.png"

# The shared interference-free predicted set = the MILP L1 @100 KB optimum, which is
# exactly what naive's predicted_metrics holds. topo/FB jsons only carry a scalar
# predicted_mean, so we reuse naive's per-metric predicted for them.
NAIVE_FN = "results/e2e_sgrid_naive_L1_100KB.json"
DISJ_FN = "results/e2e_sgrid_disjoint_L1_100KB.json"


def _own_pred(pm: dict) -> dict:
    return {"mean": pm["mean"], "geo": pm["geomean"], "max": pm["max"]}


def _true_metrics(tm: dict) -> dict:
    return {"mean": tm["mean"],
            "geo": tm.get("geomean", tm.get("geo")),
            "max": tm.get("max")}


def load():
    shared = _own_pred(json.loads((ROOT / NAIVE_FN).read_text())["predicted_metrics"])
    disj_pred = _own_pred(json.loads((ROOT / DISJ_FN).read_text())["predicted_metrics"])
    rows = []

    def add(label, fn, pred):
        d = json.loads((ROOT / fn).read_text())
        rows.append({"label": label,
                     "true": _true_metrics(d["true_metrics"]),
                     "pred": pred})

    add("naive coexist 279", NAIVE_FN, shared)                                    # own == shared
    add("topo-order 279", "results/e2e_true_ordered_topo_sgrid_L1_100KB.json", shared)
    add("FB-order 279", "results/e2e_true_ordered_fb_sgrid_L1_100KB.json", shared)
    add("disjoint 32", DISJ_FN, disj_pred)                                        # own (worse) pred
    return rows


def main():
    rows = load()
    names = [r["label"] for r in rows]
    colors = ["#707070", "#f0ad4e", "#2ca02c", "#9467bd"]
    fig, axes = plt.subplots(1, 3, figsize=(14, 4.8))
    for ax, met, title, ylab, logy in [
        (axes[0], "mean", "deployed mean q-error", "mean q-error", False),
        (axes[1], "geo", "deployed geometric-mean q-error", "geomean q-error", False),
        (axes[2], "max", "deployed max per-query q-error", "max q-error", True),
    ]:
        x = range(len(names))
        pdv = [r["pred"][met] for r in rows]
        trv = [r["true"][met] for r in rows]
        ax.bar([i - 0.18 for i in x], pdv, 0.36, label="predicted (intf-free)",
               color="#fed976", edgecolor="k")
        ax.bar([i + 0.18 for i in x], trv, 0.36, label="TRUE deployed",
               color=colors, edgecolor="k")
        ax.axhline(1.0, color="black", ls=":", lw=0.9, alpha=0.45)
        ax.set_xticks(list(x)); ax.set_xticklabels(names, rotation=24, ha="right", fontsize=7.5)
        ax.set_title(title, fontsize=10)
        if logy:
            ax.set_yscale("log")
        ax.set_ylabel(ylab)
        ax.grid(axis="y", alpha=0.25)
    axes[0].legend(fontsize=8, loc="upper right")
    # FB-order's TRUE bar is at x-index 2 (low tail); naive/disjoint tails stay high.
    axes[2].annotate("FB-order closes the\npred/true tail gap", xy=(2, 18.9),
                     xytext=(0.6, 500), fontsize=8,
                     arrowprops=dict(arrowstyle="->", lw=1.0))
    fig.suptitle("E2E deploy (S-grid census/L1/100KB): TRUE q-error vs predicted (per metric)\n"
                 "census climate · L1(S=300k) · 100 KB · one shared ANALYZE per deploy",
                 fontsize=11)
    fig.tight_layout(rect=(0, 0, 1, 0.90))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(OUT, dpi=150)
    print("wrote", OUT)
    print("\nstrategy                   pred(mean)  TRUE mean    geo   max(TRUE)")
    for r in rows:
        print(f"{r['label']:24s} {r['pred']['mean']:9.3f}  {r['true']['mean']:9.3f}  "
              f"{r['true']['geo']:.3f}  {r['true']['max']:8.1f}")


if __name__ == "__main__":
    main()
