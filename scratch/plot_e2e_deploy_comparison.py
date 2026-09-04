"""Grouped bar chart: TRUE deployed q-error vs per-metric predicted, 4 strategies.

census climate, L1, 100 KB, full-468 EXPLAIN per deploy.
Strategies share the same interference-free predicted (from the MILP L1 solve),
EXCEPT Option-A disjoint which predicts its own (lower quality).
Each panel shows, for that metric, predicted vs TRUE side by side.
Panels: mean / geometric-mean / max (log-y for max).
"""
from __future__ import annotations
import json
from pathlib import Path
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "results" / "figures" / "e2e_deploy_comparison.png"

# interference-free prediction for the 282-set (from milp_effect_time L1 @100KB)
PRED282 = {"mean": 1.4041, "geo": 1.2094, "max": 14.23}

# strategy -> (label, json, true_metrics_key, predicted_metrics_dict_or_key)
STRATS = [
    ("naive coexist 282", "results/e2e_deploy_L1_100KB.json", PRED282),
    ("topo-order 282", "results/e2e_true_ordered_L1_100KB.json", PRED282),
    ("FB-order 282", "results/e2e_true_ordered_fb_L1_100KB.json", PRED282),
]


def load():
    rows = []
    for label, fn, pred in STRATS:
        d = json.loads((ROOT / fn).read_text())
        tm = d["true_metrics"]
        # Option-A disjoint reads its own predicted metrics (else above dict)
        if "disj" in fn or "Option" in label:
            pm = d.get("predicted_metrics")
            pred = {"mean": pm["mean"], "geo": pm["geomean"], "max": pm["max"]}
        rows.append({"label": label,
                     "true": {"mean": tm["mean"],
                              "geo": tm.get("geomean", tm.get("geo")),
                              "max": tm.get("max")},
                     "pred": pred})
    # add Option-A disjoint separately with its own predicted metrics
    fd = json.loads((ROOT / "results" / "e2e_deploy_L1_100KB_disjoint.json").read_text())
    pm = fd["predicted_metrics"]; tm = fd["true_metrics"]
    rows.append({"label": "Option-A disjoint 33",
                 "true": {"mean": tm["mean"], "geo": tm.get("geomean"), "max": tm.get("max")},
                 "pred": {"mean": pm["mean"], "geo": pm["geomean"], "max": pm["max"]}})
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
    axes[2].annotate("FB-order closes the\npred/true tail gap", xy=(2, 18.6),
                     xytext=(0.6, 500), fontsize=8,
                     arrowprops=dict(arrowstyle="->", lw=1.0))
    fig.suptitle("Full-468 E2E deploy: TRUE q-error vs predicted (per metric)\n"
                 "census climate · L1 · 100 KB · one shared ANALYZE per deploy",
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
