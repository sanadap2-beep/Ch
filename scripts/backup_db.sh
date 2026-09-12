#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
#  backup_db.sh — dump the database (+ optionally the media folder)
#  usage: ./scripts/backup_db.sh [destination-dir] [--no-media] [--keep N]
#  PostgreSQL: pg_dump custom format.  SQLite: file copy + integrity check.
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

DEST="${1:-$ROOT/backups}"
shift || true
WITH_MEDIA=1
KEEP=14
for arg in "$@"; do
  case "$arg" in
    --no-media) WITH_MEDIA=0 ;;
    --keep)     : ;;                 # value consumed below
    --keep=*)   KEEP="${arg#*=}" ;;
    *)          KEEP="$arg" ;;
  esac
done

mkdir -p "$DEST"
STAMP="$(date -u +%Y%m%d-%H%M%S)"

# read DATABASE_URL from the environment or .env
DB_URL="${DATABASE_URL:-}"
if [ -z "$DB_URL" ] && [ -f .env ]; then
  DB_URL="$(grep -E '^DATABASE_URL=' .env | tail -1 | cut -d= -f2- | tr -d '"')"
fi
[ -n "$DB_URL" ] || { echo "DATABASE_URL not set" >&2; exit 1; }

if [[ "$DB_URL" == postgresql* ]]; then
  # postgresql+asyncpg://user:pass@host:port/db  →  strip the driver
  PG_URL="${DB_URL/+asyncpg/}"
  OUT="$DEST/db-$STAMP.dump"
  echo "→ pg_dump → $OUT"
  pg_dump --format=custom --no-owner --no-privileges "$PG_URL" -f "$OUT"
else
  FILE="${DB_URL#*://}"
  [ -f "$FILE" ] || { echo "sqlite file not found: $FILE" >&2; exit 1; }
  OUT="$DEST/db-$STAMP.sqlite"
  echo "→ copying $FILE → $OUT"
  if command -v sqlite3 >/dev/null 2>&1; then
    sqlite3 "$FILE" ".backup '$OUT'"
    sqlite3 "$OUT" "PRAGMA integrity_check;" | head -1
  else
    cp "$FILE" "$OUT"
  fi
fi

if [ "$WITH_MEDIA" -eq 1 ] && [ -d storage/media ]; then
  MEDIA_OUT="$DEST/media-$STAMP.tar.gz"
  echo "→ media → $MEDIA_OUT"
  tar -czf "$MEDIA_OUT" -C storage media
fi

# prune old backups
if command -v ls >/dev/null 2>&1; then
  ls -1t "$DEST"/db-* 2>/dev/null | tail -n +"$((KEEP + 1))" | while read -r old; do
    echo "→ pruning $old"; rm -f "$old"
  done
fi

echo "✔ backup complete in $DEST"
