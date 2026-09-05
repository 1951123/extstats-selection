"""Migrate the PostgreSQL DMV single-table bench table onto Oracle 23ai.

The BayesCard/AreCELearnedYet DMV NY-vehicle bench is PG-only today
(``dmv`` DB, table ``public.dmv``, 11.6M rows x 11 categorical text columns).
We copy it verbatim PG -> Oracle (SYSTEM schema, table ``DMV``) so the
DMV benchmark can run under Protocol-A on Oracle — specifically so we can
probe whether Oracle AUTO_SAMPLE_SIZE still means full-scan on a genuinely
large (~11.6M-row) table, or whether the engine finally switches to partial
automatic sampling at scale.

Method: stream PG rows with a server-side cursor (batched fetch), then
``executemany`` into Oracle in chunks. Oracle columns are VARCHAR2 sized to the
measured max per-column length (plus headroom); values copied as-is (no trim/
case change) so Oracle rows == PG rows byte-for-byte and every DMV query's
ground-truth reproduces identically.

Idempotent: drops+recreates Oracle DMV. Safe to re-run.

Usage:  python scratch/load_dmv_to_oracle.py             # full 11.6M
        python scratch/load_dmv_to_oracle.py --limit 1000 # smoke
"""
from __future__ import annotations

import argparse
import sys
import time

import oracledb
import psycopg

PG = dict(host="localhost", port=5432, user="postgres", password="postgres",
          dbname="dmv")
OR = dict(user="SYSTEM", password="lxf82073077", dsn="localhost:1521/FREEPDB1")

# (Oracle-col, PG-maxlen, width with headroom)
COLS = [
    ("RECORD_TYPE", 4, 16),
    ("REGISTRATION_CLASS", 3, 16),
    ("STATE", 2, 8),
    ("COUNTY", 11, 32),
    ("BODY_TYPE", 4, 16),
    ("FUEL_TYPE", 8, 16),
    ("REG_VALID_DATE", 8, 16),
    ("COLOR", 5, 32),
    ("SCOFFLAW_INDICATOR", 1, 4),
    ("SUSPENSION_INDICATOR", 1, 4),
    ("REVOCATION_INDICATOR", 1, 4),
]
ORACLE_COLS = [c[0] for c in COLS]

BATCH = 50_000


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0,
                    help="0 = full; else only insert first N PG rows (smoke)")
    args = ap.parse_args()

    oc = oracledb.connect(**OR)
    oc.autocommit = False
    ocur = oc.cursor()

    # 1) create (drop) table
    ocur.execute("BEGIN EXECUTE IMMEDIATE 'DROP TABLE SYSTEM.DMV'; "
                 "EXCEPTION WHEN OTHERS THEN IF SQLCODE != -942 THEN RAISE; "
                 "END IF; END;")
    coldefs = ", ".join(f'"{cn}" VARCHAR2({wd})' for cn, _, wd in COLS)
    ocur.execute(f'CREATE TABLE SYSTEM.DMV ({coldefs})')
    print(f"[oracle] created SYSTEM.DMV ({len(COLS)} cols)", flush=True)

    # 2) stream PG -> batch insert Oracle
    t0 = time.time()
    total = 0
    ins_sql = (f"INSERT INTO SYSTEM.DMV ({', '.join(ORACLE_COLS)}) "
               f"VALUES ({', '.join([':%d' % (i + 1) for i in range(len(COLS))])})")
    pgone = ", ".join(c0.lower() for c0 in ORACLE_COLS)
    limit_sql = f" LIMIT {int(args.limit)}" if args.limit else ""
    with psycopg.connect(**PG, row_factory=psycopg.rows.dict_row) as pconn:
        with pconn.cursor(name="dmv_migrate") as pcur:
            pcur.itersize = BATCH
            pcur.execute(f"SELECT {pgone} FROM public.dmv{limit_sql}")
            while True:
                rows = pcur.fetchmany(BATCH)
                if not rows:
                    break
                batch = [[("" if r[c0.lower()] is None else r[c0.lower()])
                          for c0 in ORACLE_COLS] for r in rows]
                try:
                    ocur.executemany(ins_sql, batch)
                    oc.commit()
                    total += len(batch)
                except oracledb.Error as e:
                    print("batch error:", str(e)[:300], "rows in batch",
                          len(batch), flush=True)
                    raise
                if total % (BATCH * 5) == 0:
                    print(f"  ...{total:,} rows ({time.time()-t0:.0f}s)",
                          flush=True)
    # count
    ocur.execute("SELECT count(*) FROM SYSTEM.DMV")
    n = ocur.fetchone()[0]
    print(f"[oracle] loaded {n:,} rows in {time.time()-t0:.0f}s", flush=True)
    oc.close()


if __name__ == "__main__":
    sys.exit(main())
