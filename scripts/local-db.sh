#!/usr/bin/env bash
# Fallback for machines without Docker: provision the spendctl role and
# databases on an already-running local Postgres 16.
set -euo pipefail

pg_isready -q || { echo "No local Postgres is accepting connections." >&2; exit 1; }

psql -d postgres -qtAc "SELECT 1 FROM pg_roles WHERE rolname='spendctl'" | grep -q 1 \
  || psql -d postgres -qc "CREATE ROLE spendctl LOGIN PASSWORD 'spendctl' CREATEDB"

for db in spendctl spendctl_test; do
  psql -d postgres -qtAc "SELECT 1 FROM pg_database WHERE datname='$db'" | grep -q 1 \
    || psql -d postgres -qc "CREATE DATABASE $db OWNER spendctl"
done
echo "Local Postgres ready (spendctl, spendctl_test)."
