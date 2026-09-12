#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
#  run.sh — start the bot (polling by default, webhook if WEBHOOK_URL is set)
#  usage: ./scripts/run.sh [--check]
#    --check   validate config + imports + dispatcher, then exit (no Telegram calls)
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# shellcheck disable=SC1091
[ -f .venv/bin/activate ] && source .venv/bin/activate

mkdir -p storage/media storage/tmp logs

if [ "${1:-}" = "--check" ]; then
  echo "→ config + dispatcher smoke test"
  python - <<'PY'
import asyncio, sys

from app.bot import build_dispatcher, create_bot
from app.config import settings
from app.handlers import routers

print(f"  bot token      : {'set' if settings.bot_token else 'MISSING'}")
print(f"  nanogpt key    : {'set' if settings.nanogpt_api_key else 'MISSING'}")
print(f"  database       : {settings.database_url.split('@')[-1]}")
print(f"  storage        : {settings.storage_backend}")
print(f"  admins         : {settings.admin_ids or 'MISSING'}")
print(f"  global cap/day : ${settings.global_daily_usd_cap}")

dp = build_dispatcher(create_bot("1:TEST"))
total = sum(len(r.message.handlers) + len(r.callback_query.handlers) for r in routers)
print(f"  routers        : {len(routers)}  handlers: {total}")

from app.db.base import dispose_engine, init_db
try:
    asyncio.run(init_db())
    print("  database       : reachable ✔")
except Exception as exc:                      # noqa: BLE001
    print(f"  database       : UNREACHABLE ✖  ({type(exc).__name__}: {str(exc)[:90]})")
    print("                 → check DATABASE_URL in .env, or run: alembic upgrade head")
finally:
    asyncio.run(dispose_engine())

missing = [n for n, ok in (("BOT_TOKEN", settings.bot_token),
                           ("NANOGPT_API_KEY", settings.nanogpt_api_key),
                           ("ADMIN_IDS", settings.admin_ids)) if not ok]
if missing:
    print(f"  ⚠️  not configured: {', '.join(missing)}")
    sys.exit(1)
PY
  exit 0
fi

echo "→ starting AI coach bot (Ctrl+C to stop)"
exec python -m app.main
