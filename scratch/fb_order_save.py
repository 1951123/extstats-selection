"""Run weighted-feedback-arc Phase-2 order on census and save results for deploy.
"""
from __future__ import annotations
import sys, json
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scratch"))
from _pg_order_ctx import load_ctx
from extstats2.core.optimize_pg_order import solve_pg_order_fb


def main(out):
    ctx = load_ctx(level="1", budget=100000)
    sol = solve_pg_order_fb(ctx["chosen_list"], ctx["base_by_qid"], ctx["applicable"])
    # sanity: a few bad queries' served realized
    for q in ("query.61", "query.274", "query.465", "query.59", "query.403", "query.335"):
        ev = sol["realised"].get(q)
        print(f"{q}: realised_served_e={ev if ev is None else round(ev,2)}")
    json.dump({"predicted_mean": ctx["predicted_mean"],
               "realised_model_mean": sol["realised_mean"],
               "n_feedback_cut": sol.get("n_feedback_cut"),
               "acyclic": sol["acyclic"],
               "chosen_colsets": len(ctx["chosen_list"]),
               "total_bytes": ctx["total_bytes"],
               "order": sol["order"]}, open(out, "w"), indent=1)
    print(f"realised_mean={sol['realised_mean']:.4f} n_cut={sol.get('n_feedback_cut')} "
          f"pred={ctx['predicted_mean']:.4f} wrote {out}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else str(ROOT / "results" / "e2e_order_fb_L1_100KB.json"))
