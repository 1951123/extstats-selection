"""Run MILP-effect(budget sweep) for all PG S-grid benches x {L0,L1}.

One (bench,level) per config, each writes its own json under results/.
Serial (each solve is only 1-8s; builds dominate). Intended to run detached:
    nohup .venv/bin/python -u scratch/measure_milp_effect_all.py > scratch/logs_sgrid/milp_effect_all.log 2>&1 &
"""
from __future__ import annotations
import subprocess, sys, time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUDGETS = "2000,5000,10000,20000,50000,100000,200000,400000"
PY = str(ROOT / ".venv" / "bin" / "python")
SC = str(ROOT / "scratch" / "measure_milp_effect_time.py")
CORPUS = str(ROOT / "results" / "per_lambda")

jobs = []
for bench in ["census", "stats_ceb_single", "dmv"]:
    for lv in [0, 1]:
        jobs.append((bench, lv))

for bench, lv in jobs:
    out = str(ROOT / "results" / f"milp_effect_sgrid_{bench}_L{lv}.json")
    print(f"[run] bench={bench} L{lv} -> {out}", flush=True)
    t0 = time.time()
    r = subprocess.run([PY, "-u", SC, "--bench", bench, "--level", str(lv),
                        "--budgets", BUDGETS, "--corpus", CORPUS, "--out", out],
                       capture_output=True, text=True)
    print(r.stdout[-2000:], flush=True)
    if r.returncode != 0:
        print(f"[run] ERROR bench={bench} L{lv} rc={r.returncode}\n{r.stderr[-2000:]}", flush=True)
    print(f"[run] bench={bench} L{lv} done in {(time.time()-t0)/60:.1f}m", flush=True)

print("[all] DONE", flush=True)
