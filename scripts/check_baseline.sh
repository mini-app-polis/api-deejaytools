#!/usr/bin/env bash
# Check that migrations/001_baseline.sql is the schema drizzle leaves behind
# (deejaytools-api ADR-008 point 2). Required, not optional: the baseline is
# only as good as this comparison.
#
# Builds two throwaway databases on one Postgres server:
#   <prefix>_drizzle   every file in conformance/drizzle/, in journal order,
#                      each in its own transaction (what drizzle-kit migrate
#                      does, minus its own `drizzle` bookkeeping schema)
#   <prefix>_baseline  migrations/001_baseline.sql alone
# then dumps the `public` schema of each and fails on any difference.
#
# Usage:
#   PGURL=postgres://postgres:postgres@localhost:5432 scripts/check_baseline.sh
#
# PG_DUMP overrides the pg_dump command. pg_dump refuses a server newer than
# itself, so CI runs the one inside the Postgres service container instead
# (see .github/workflows/ci.yml).
#
# Regenerating the baseline (only ever from drizzle's result, never by hand):
#   REGENERATE=1 PGURL=... scripts/check_baseline.sh

set -euo pipefail

PGURL="${PGURL:-postgres://postgres:postgres@localhost:5432}"
# drizzle's long constraint names draw truncation NOTICEs; they are expected.
export PGOPTIONS="${PGOPTIONS:--c client_min_messages=warning}"
PG_DUMP="${PG_DUMP:-pg_dump}"
PREFIX="${PREFIX:-deejaytools_baseline_check}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DRIZZLE_DIR="$ROOT/conformance/drizzle"
BASELINE="$ROOT/migrations/001_baseline.sql"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

DRIZZLE_DB="${PREFIX}_drizzle"
BASELINE_DB="${PREFIX}_baseline"

admin() { psql "$PGURL/postgres" -v ON_ERROR_STOP=1 -q -X "$@"; }

recreate() {
  admin -c "DROP DATABASE IF EXISTS \"$1\"" -c "CREATE DATABASE \"$1\""
}

# Schema only, public only, without anything that varies by who ran it.
dump() {
  $PG_DUMP --dbname="$PGURL/$1" --schema-only --schema=public \
    --no-owner --no-privileges --no-comments --quote-all-identifiers |
    grep -v -E '^(--|SET |SELECT pg_catalog\.set_config|\\(un)?restrict )' |
    sed -e '/^CREATE SCHEMA "public";$/d' |
    cat -s
}

recreate "$DRIZZLE_DB"
python3 - "$DRIZZLE_DIR/meta/_journal.json" > "$WORK/order" <<'EOF'
import json, sys
for entry in sorted(json.load(open(sys.argv[1]))["entries"], key=lambda e: e["idx"]):
    print(entry["tag"] + ".sql")
EOF
while read -r file; do
  psql "$PGURL/$DRIZZLE_DB" -v ON_ERROR_STOP=1 -q -X --single-transaction \
    -f "$DRIZZLE_DIR/$file" > /dev/null
done < "$WORK/order"
dump "$DRIZZLE_DB" > "$WORK/drizzle.sql"

if [[ "${REGENERATE:-}" == "1" ]]; then
  {
    cat <<'HEADER'
-- Migration 001: baseline
--
-- The deejaytools schema exactly as deejaytools-api's drizzle migrations
-- 0000-0015 leave it, enums, constraints, indexes and recorded drift
-- included (deejaytools-api ADR-008 point 2). Generated, never edited:
--
--   REGENERATE=1 scripts/check_baseline.sh
--
-- Production adopts this file without running it. The first deploy of this
-- service sets BOOTSTRAP_MIGRATIONS=true and BOOTSTRAP_EXCLUDE naming every
-- later file, so the runner records 001 as applied and runs the rest.
-- Test databases are built from it, and CI checks it against drizzle's
-- result on every run (scripts/check_baseline.sh).
--
-- Only the `public` schema. drizzle's own `drizzle` schema and its journal
-- stay in production untouched until deejaytools-api is retired.

HEADER
    cat "$WORK/drizzle.sql"
  } > "$BASELINE"
  echo "Wrote $BASELINE"
fi

recreate "$BASELINE_DB"
psql "$PGURL/$BASELINE_DB" -v ON_ERROR_STOP=1 -q -X --single-transaction \
  -f "$BASELINE" > /dev/null
dump "$BASELINE_DB" > "$WORK/baseline.sql"

admin -c "DROP DATABASE \"$DRIZZLE_DB\"" -c "DROP DATABASE \"$BASELINE_DB\""

if diff -u "$WORK/drizzle.sql" "$WORK/baseline.sql"; then
  echo "001_baseline.sql matches the schema drizzle builds."
else
  echo "::error::001_baseline.sql differs from the schema drizzle builds (diff above)." >&2
  exit 1
fi
