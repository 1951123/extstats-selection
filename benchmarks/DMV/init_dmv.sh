#!/usr/bin/env bash
# Reproducible DMV benchmark ingestion (BayesCard / AreCELearnedYet DMV dataset).
#
# DMV is the New York DMV vehicle registrations dataset (11.6M rows). Data is
# public at catalog.data.gov but continuously updated; BayesCard's bundled
# query.sql ground-truths were computed on the AUTHOR's snapshot and do NOT
# reproduce on the public copy (~75% of the 1965 rows differ by >1%). Per the
# BayesCard repo note we therefore "以数据为准": we ingest OUR snapshot and
# RECOMPUTE every query's true cardinality on it.
#
# Requires: the raw CSV at benchmarks/DMV/data/original.csv (this repo's copy),
#           a local PostgreSQL, PGPASSWORD=postgres.
#
# Usage:
#   bash benchmarks/DMV/init_dmv.sh          # create db, load, trim, analyze
#   .venv/bin/python -u scratch/gen_dmv_truths.py --workers 8   # recompute truths -> queries/dmv.sql
set -euo pipefail
cd "$(dirname "$0")/.."
DB=dmv
CSV=benchmarks/DMV/data/original.csv
echo "== creating/filling PostgreSQL DB '$DB' from $CSV =="
PGPASSWORD=postgres psql -h localhost -U postgres -c "DROP DATABASE IF EXISTS $DB" >/dev/null
PGPASSWORD=postgres psql -h localhost -U postgres -c "CREATE DATABASE $DB" >/dev/null
PGPASSWORD=postgres psql -h localhost -U postgres -d $DB <<'SQL'
CREATE TABLE DMV (
  Record_Type text, Registration_Class text, State text, County text,
  Body_Type text, Fuel_Type text, Reg_Valid_Date text, Color text,
  Scofflaw_Indicator text, Suspension_Indicator text, Revocation_Indicator text
);
\COPY DMV FROM '$CSV' WITH (FORMAT csv, HEADER true)
SQL
# BayesCard CSV right-pads categorical values; the DSL queries use unpadded
# tokens, so canonicalize (trim) so IN/eq predicates match.
PGPASSWORD=postgres psql -h localhost -U postgres -d $DB <<'SQL'
UPDATE DMV SET
  Record_Type=btrim(Record_Type), Registration_Class=btrim(Registration_Class),
  State=btrim(State), County=btrim(County), Body_Type=btrim(Body_Type),
  Fuel_Type=btrim(Fuel_Type), Color=btrim(Color),
  Scofflaw_Indicator=btrim(Scofflaw_Indicator),
  Suspension_Indicator=btrim(Suspension_Indicator),
  Revocation_Indicator=btrim(Revocation_Indicator);
ANALYZE DMV;
SELECT count(*) AS dmv_rows FROM DMV;
SQL
echo "== done. Next recompute query truths with: =="
echo "   .venv/bin/python -u scratch/gen_dmv_truths.py --workers 8"
