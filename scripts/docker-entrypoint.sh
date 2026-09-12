#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
#  docker-entrypoint.sh — apply migrations, then hand over to the bot.
#  Skips migrations when SKIP_MIGRATIONS=1 (e.g. when several replicas boot at
#  once and one dedicated job owns the schema).
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

if [ "${SKIP_MIGRATIONS:-0}" = "1" ]; then
  echo "→ SKIP_MIGRATIONS=1 — not touching the schema"
else
  echo "→ waiting for the database…"
  for attempt in $(seq 1 30); do
    if alembic current >/dev/null 2>&1; then
      break
    fi
    if [ "$attempt" -eq 30 ]; then
      echo "✖ database never became reachable (DATABASE_URL=${DATABASE_URL%%@*}@…)" >&2
      exit 1
    fi
    sleep 2
  done

  echo "→ alembic upgrade head"
  alembic upgrade head
fi

echo "→ starting: $*"
exec "$@"
