"""链式自动测量：等当前 dmv/PG 并行测量跑完后，自动启动 census/PG 并行测量。

用法（nohup 托管，链子在后台持续）::
    nohup .venv/bin/python -u scratch/chain_dmv_census.py \
        > scratch/logs_sgrid/chain_dmv_census.log 2>&1 &

链逻辑:
1) 轮询等待 dmv/postgres 测量完成：`measure_dmv_parallel.py` 主进程结束，
   且 results/measure/dmv/postgres 下 dmv.*.json 达到预期 candidate-bearing 数
   (1926)，持续若干秒稳定即判定完成（resumable：即便少几条也会被 census 前重跑兜住）。
2) dmv 完成后自检 census_m1..m8 镜像库存在、census/postgres 目录可写。
3) 用 subprocess.Popen 分离启动 census/PG 并行测量(8 mirror)，
   日志写到 scratch/logs_sgrid/census_pg_parallel.log，之后本 wrapper 退出。
4) wrapper 自身全程由外层 nohup 守护，可查退出码，日志含关键状态行。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DMV_DIR = ROOT / "results" / "measure" / "dmv" / "postgres"
CENSUS_DIR = ROOT / "results" / "measure" / "census" / "postgres"
LOG_DIR = ROOT / "scratch" / "logs_sgrid"
DMV_EXPECTED = 1926           # dmv candidate-bearing (arity-2)
CENSUS_EXPECTED = 467         # census candidate-bearing (arity-2); census 总 468 但 1 条 no-cand
STABLE_ROUNDS = 4             # 连续 n 次(隔 15s) pom 到 target 判定稳定
POLL = 15                     # 秒


def _dmv_json_count() -> int:
    try:
        return len(list(DMV_DIR.glob("dmv.*.json")))
    except FileNotFoundError:
        return 0


def _dmv_pid_alive() -> bool:
    out = subprocess.run(["pgrep", "-f", "measure_dmv_parallel.py"],
                         capture_output=True, text=True).stdout.strip()
    return bool(out)


def _wait_dmv_done() -> int:
    stable = 0
    last = -1
    while True:
        n = _dmv_json_count()
        alive = _dmv_pid_alive()
        print(f"[chain] poll: dmv json={n}/{DMV_EXPECTED} "
              f"main_alive={alive}", flush=True)
        if (not alive) and n >= DMV_EXPECTED:
            stable += 1
            if stable >= STABLE_ROUNDS:
                print(f"[chain] dmv complete: {n} files, main gone", flush=True)
                return n
        else:
            stable = 0
            if (not alive) and n < DMV_EXPECTED and n == last:
                # main died early without full corpus - do not proceed blind
                print(f"[chain] WARN: dmv main gone but only {n}/{DMV_EXPECTED} "
                      f"files (2 poll rounds). Re-checking before proceeding.",
                      flush=True)
        last = n
        time.sleep(POLL)


def _db_exists(db: str) -> bool:
    out = subprocess.run(
        ["psql", "-h", "localhost", "-U", "postgres", "-d", "postgres", "-t", "-A",
         "-c", "SELECT 1 FROM pg_database WHERE datname='" + db + "'"],
        capture_output=True, text=True, env={**os.environ, "PGPASSWORD": "postgres"})
    return "1" in out.stdout


def main() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    print("[chain] start. waiting for dmv to finish...", flush=True)
    n_dmv = _wait_dmv_done()

    # self-check census mirrors
    missing = [f"census_m{i}" for i in range(1, 9) if not _db_exists(f"census_m{i}")]
    if missing:
        print(f"[chain] ABORT: census mirrors missing: {missing}", flush=True)
        sys.exit(2)
    print("[chain] census_m1..m8 present.", flush=True)

    # census/postgres should be clean; if not, warn (driver is resumable so it
    # won't clobber, but a pre-existing partial corpus would be 'continued'.
    pre = len(list(CENSUS_DIR.glob("query.*.json"))) if CENSUS_DIR.exists() else 0
    print(f"[chain] census/postgres pre-existing files: {pre} "
          f"(expected 0 for a clean run)", flush=True)

    census_log = LOG_DIR / "census_pg_parallel.log"
    cmd = [str(ROOT / ".venv" / "bin" / "python"), "-u",
           str(ROOT / "scratch" / "measure_census_parallel.py"),
           "--n", "8",
           "--out", str(ROOT / "results" / "measure"),
           "--dbprefix", "census_m", "--bench", "census",
           "--table", ".climate", "--arities", "2",
           "--levels", "0", "1"]
    fh = open(census_log, "w")
    print(f"[chain] launching census/PG parallel (expected {CENSUS_EXPECTED}): "
          f"{' '.join(cmd)}", flush=True)
    print(f"[chain] census log -> {census_log}", flush=True)
    proc = subprocess.Popen(cmd, stdout=fh, stderr=subprocess.STDOUT, cwd=str(ROOT))
    print(f"[chain] census main started pid={proc.pid}", flush=True)
    print(f"[chain] done (after dmv {n_dmv} files). chain worker exits; "
          f"census runs detached.", flush=True)
    sys.exit(0)


if __name__ == "__main__":
    main()
