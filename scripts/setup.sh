#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
#  setup.sh — one-shot developer/operator bootstrap
#  usage: ./scripts/setup.sh [--no-migrate] [--dev]
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

MIGRATE=1
DEV=0
for arg in "$@"; do
  case "$arg" in
    --no-migrate) MIGRATE=0 ;;
    --dev)        DEV=1 ;;
    -h|--help)    sed -n '2,6p' "$0"; exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

PY="${PYTHON:-python3.11}"
command -v "$PY" >/dev/null 2>&1 || PY=python3
echo "→ python: $("$PY" -V 2>&1)"

# ── virtualenv ───────────────────────────────────────────────────────────────
if [ ! -d .venv ]; then
  echo "→ creating .venv"
  "$PY" -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate
python -m pip install --upgrade pip wheel >/dev/null

echo "→ installing requirements"
pip install -q -r requirements.txt
if [ "$DEV" -eq 1 ]; then
  echo "→ installing dev requirements"
  pip install -q -r requirements-dev.txt
  pip install -q ruff arabic-reshaper python-bidi fonttools
fi

# ── runtime directories ──────────────────────────────────────────────────────
mkdir -p storage/media storage/tmp assets/fonts docs scripts
echo "→ storage/media, storage/tmp ready"

# ── environment ──────────────────────────────────────────────────────────────
if [ ! -f .env ]; then
  cp .env.example .env
  echo "→ created .env from .env.example  (⚠️  set BOT_TOKEN, NANOGPT_API_KEY, ADMIN_IDS, DATABASE_URL)"
else
  echo "→ .env already exists (left untouched)"
fi

# ── database ─────────────────────────────────────────────────────────────────
if [ "$MIGRATE" -eq 1 ]; then
  echo "→ alembic upgrade head"
  alembic upgrade head
else
  echo "→ skipping migrations (--no-migrate)"
fi

cat <<'TXT'

✔ setup complete
  next:
    1) edit .env          (BOT_TOKEN · NANOGPT_API_KEY · ADMIN_IDS · DATABASE_URL)
    2) pytest -q          (196 tests, no network needed)
    3) python -m app.main (start the bot)
TXT
