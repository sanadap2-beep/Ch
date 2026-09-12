# ─────────────────────────────────────────────────────────────────────────────
#  AI Fitness & Nutrition Coach — Telegram bot
#  Build:  docker build -t ai-coach .
#  Run:    docker run --env-file .env -v ai-coach-media:/app/storage/media ai-coach
# ─────────────────────────────────────────────────────────────────────────────
FROM python:3.11-slim AS base

# Pillow needs libjpeg/zlib; DejaVu is the fallback font for Arabic charts & PDFs.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libjpeg62-turbo zlib1g fonts-dejavu-core postgresql-client \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependencies first so code changes do not invalidate the layer cache
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p storage/media storage/tmp logs \
    && useradd --create-home --uid 10001 coach \
    && chown -R coach:coach /app
USER coach

# Fail fast on a broken image: imports + dispatcher + config
HEALTHCHECK --interval=60s --timeout=10s --start-period=20s --retries=3 \
    CMD python -c "import app.main" || exit 1

# Migrations are applied by the entrypoint before the bot starts polling.
ENTRYPOINT ["./scripts/docker-entrypoint.sh"]
CMD ["python", "-m", "app.main"]
