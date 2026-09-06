"""Run the unified MILP storage+maint sweeps for all three benches.

Serial wrapper around scratch/measure_milp_curve.py; writes (per bench x kind):
  results/milp_storage_sgrid_<bench>.json
  results/milp_maint_sgrid_<bench>.json
Run detached:
  nohup .venv/bin/python -u scratch/measure_milp_curve_all.py \
      > scratch/logs_sgrid/milp_curve_all.log 2>&1 &
"""
from __future__ import annotations
import subprocess, sys, time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = str(ROOT / ".venv" / "bin" / "python")
SC = str(ROOT / "scratch" / "measure_milp_curve.py")

STORAGE_GRID = "2000,5000,10000,20000,50000,100000,200000,400000"
MAINT_GRID = "0.1,0.25,0.5,0.8,1.0,2.0,2.56,3.0,4.0,6.0,8.0"

jobs = []
for bench in ["census", "dmv", "stats_ceb_single"]:
    jobs.append((bench, "storage", STORAGE_GRID))
    jobs.append((bench, "maint", MAINT_GRID))

for bench, kind, grid in jobs:
    t0 = time.time()
    print(f"[run] {bench}/{kind}", flush=True)
    r = subprocess.run([PY, "-u", SC, "--kind", kind, "--bench", bench,
                        "--grid", grid], capture_output=True, text=True)
    print(r.stdout[-1500:], flush=True)
    if r.returncode != 0:
        print(f"[run] ERROR {bench}/{kind} rc={r.returncode}\n{r.stderr[-1500:]}", flush=True)
        sys.exit(1)
    print(f"[run] {bench}/{kind} done in {(time.time()-t0)/60:.1f}m", flush=True)

print("[all] DONE", flush=True)
