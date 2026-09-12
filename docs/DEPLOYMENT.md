# 🚀 Deployment Guide

Target stack: **PostgreSQL 14+ · Python 3.11 · long polling or webhook · systemd or Docker**.
Everything below assumes the repo is at `/opt/ai-coach` (adjust as needed).

---

## 1. Server prerequisites

```bash
sudo apt update && sudo apt install -y python3.11 python3.11-venv postgresql git \
                                       libjpeg-dev zlib1g-dev fonts-dejavu-core
```

Arabic text in charts/PDFs needs an Arabic-capable font. `fonts-dejavu-core` is enough for
matplotlib; for nicer PDF output install `fonts-noto-core` and point `ARABIC_FONT_PATH` at
`/usr/share/fonts/truetype/noto/NotoNaskhArabic-Regular.ttf`.

## 2. Database

```bash
sudo -u postgres psql -c "CREATE ROLE coach LOGIN PASSWORD 'strong-password';"
sudo -u postgres psql -c "CREATE DATABASE ai_coach OWNER coach;"
```

`.env`:
```dotenv
DATABASE_URL=postgresql+asyncpg://coach:strong-password@127.0.0.1:5432/ai_coach
DB_AUTO_CREATE=false          # migrations own the schema in production
DB_ECHO=false
DB_POOL_SIZE=10
```

```bash
cd /opt/ai-coach
source .venv/bin/activate
alembic upgrade head
alembic current          # sanity check
```

> **Rollback**: `alembic downgrade -1`. Take a dump first (`scripts/backup_db.sh`).

## 3. Environment

Copy `.env.example` → `.env` and set at minimum:

```dotenv
BOT_TOKEN=123456789:AA...           # from @BotFather
NANOGPT_API_KEY=sk-ngpt-...
ADMIN_IDS=111111111,222222222       # numeric Telegram IDs
TIMEZONE=Asia/Damascus
GLOBAL_DAILY_USD_CAP=25
```

Protect it: `chmod 600 .env` and never commit it (already in `.gitignore`).

## 4. Run with systemd (recommended)

`/etc/systemd/system/ai-coach.service`:
```ini
[Unit]
Description=AI Fitness Coach Telegram Bot
After=network-online.target postgresql.service
Wants=network-online.target

[Service]
Type=simple
User=coach
WorkingDirectory=/opt/ai-coach
EnvironmentFile=/opt/ai-coach/.env
ExecStart=/opt/ai-coach/.venv/bin/python -m app.main
Restart=always
RestartSec=5
KillSignal=SIGTERM
TimeoutStopSec=30
StandardOutput=append:/var/log/ai-coach/bot.log
StandardError=append:/var/log/ai-coach/bot.err.log

[Install]
WantedBy=multi-user.target
```

```bash
sudo mkdir -p /var/log/ai-coach && sudo chown coach:coach /var/log/ai-coach
sudo systemctl daemon-reload
sudo systemctl enable --now ai-coach
sudo journalctl -u ai-coach -f          # live logs
```

The process handles SIGTERM/SIGINT cleanly (scheduler → AI client → bot → DB engine).

## 5. Run with Docker

```dockerfile
FROM python:3.11-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
        libjpeg62-turbo zlib1g fonts-dejavu-core && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
CMD ["python", "-m", "app.main"]
```

```yaml
# docker-compose.yml
services:
  db:
    image: postgres:16-alpine
    environment: { POSTGRES_USER: coach, POSTGRES_PASSWORD: strong, POSTGRES_DB: ai_coach }
    volumes: [ "pgdata:/var/lib/postgresql/data" ]
  bot:
    build: .
    env_file: .env
    environment:
      DATABASE_URL: postgresql+asyncpg://coach:strong@db:5432/ai_coach
    depends_on: [ db ]
    volumes: [ "media:/app/storage/media" ]
volumes: { pgdata: {}, media: {} }
```

Run migrations once inside the container: `docker compose run --rm bot alembic upgrade head`.

`deploy/Caddyfile` is a ready reverse proxy for webhook mode (automatic HTTPS); the
`caddy` service in `docker-compose.yml` is commented out and references it.

## 6. Webhook mode (optional)

```dotenv
WEBHOOK_URL=https://bot.example.com
WEBHOOK_PATH=/webhook
WEBHOOK_SECRET=<random-64-chars>
WEBHOOK_PORT=8443
```

`app/main.py` then registers the webhook (`delete_webhook(drop_pending_updates=True)` first) and
serves `POST /webhook` on `WEBHOOK_PORT`, validating `X-Telegram-Bot-Api-Secret-Token`.
Put nginx/Caddy in front for TLS; Telegram requires a valid certificate and an HTTPS origin.
For long polling (default) leave `WEBHOOK_URL` empty.

**Only one process may consume updates.** Scale by moving to webhook + several workers behind a
load balancer, or keep a single polling process and scale the DB instead.

## 7. Storage

* **Local (default)**: files land in `storage/media/...`. Back this directory up or mount a volume.
* **S3/R2**:
  ```dotenv
  STORAGE_BACKEND=s3
  S3_BUCKET=ai-coach-media
  S3_REGION=auto
  S3_ENDPOINT_URL=https://<account>.r2.cloudflarestorage.com
  S3_ACCESS_KEY_ID=...
  S3_SECRET_ACCESS_KEY=...
  ```
  Set `PUBLIC_MEDIA_BASE_URL` only if the bucket serves public content (it should not — media is
  user photos). Auto-purge runs daily after `MEDIA_RETENTION_DAYS`.

## 8. Redis (optional)

```dotenv
REDIS_URL=redis://127.0.0.1:6379/0
```
Used for FSM state across processes. Without it aiogram keeps state in memory (fine for one process).

## 9. Backups & monitoring

```bash
scripts/backup_db.sh /var/backups/ai-coach      # pg_dump + media tarball
# cron: 0 3 * * * /opt/ai-coach/scripts/backup_db.sh /var/backups/ai-coach
```

Watch:
* `journalctl -u ai-coach | grep -iE "error|traceback"`
* 🛡️ → 🤖 استهلاك API: success rate should stay > 95%; cost vs `GLOBAL_DAILY_USD_CAP`.
* Postgres connections (`DB_POOL_SIZE` × workers must stay under `max_connections`).
* Disk usage of `storage/media` (retention should keep it flat).

## 10. Upgrading

```bash
cd /opt/ai-coach
git fetch && git checkout <tag-or-branch> && git pull
source .venv/bin/activate
pip install -r requirements.txt
alembic upgrade head
sudo systemctl restart ai-coach
pytest -q            # optional smoke test against a staging DB
```

Never edit the schema by hand — add an Alembic revision (`alembic revision --autogenerate -m "…"`),
review it, then `alembic upgrade head`.

## 11. Cost control checklist

1. Start with a low `GLOBAL_DAILY_USD_CAP` (e.g. `5`) and raise it as traffic grows.
2. Check 🛡️ → 🧩 توجيه الموديلات weekly: move cheap tasks to cheaper models (`/route`).
3. Keep `FREE_DAILY_CONSULTS` and `FREE_DAILY_PHOTO_SCANS` small — they are unbilled AI calls.
4. `USER_DAILY_POINTS_CAP` / `USER_MONTHLY_POINTS_CAP` protect a single user from runaway spend.
5. When the cap is hit users see a friendly message (`budget.hit`) and are **not** charged.
