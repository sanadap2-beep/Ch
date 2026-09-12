"""Logging setup: concise console logs, quieter third-party loggers."""
from __future__ import annotations

import logging
import os
import sys

FORMAT = "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# libraries that are noisy but rarely actionable
QUIET = {
    "aiogram": logging.INFO,
    "aiogram.event": logging.WARNING,
    "aiogram.dispatcher.middlewares": logging.WARNING,
    "httpx": logging.WARNING,
    "httpcore": logging.WARNING,
    "apscheduler": logging.WARNING,
    "sqlalchemy.engine": logging.INFO,
    "PIL": logging.WARNING,
    "matplotlib": logging.WARNING,
    "asyncio": logging.WARNING,
    "boto3": logging.WARNING,
    "botocore": logging.WARNING,
}


def configure_logging(level: str | None = None, *, json_logs: bool = False) -> None:
    """Idempotent logging configuration used by the app and by scripts/tests."""
    from app.config import settings

    resolved = (level or settings.log_level or os.getenv("LOG_LEVEL") or "INFO").upper()
    root = logging.getLogger()
    root.setLevel(getattr(logging, resolved, logging.INFO))

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(FORMAT, datefmt=DATE_FORMAT))
    root.handlers = [handler]

    for name, verbosity in QUIET.items():
        logging.getLogger(name).setLevel(max(verbosity, root.level))

    if settings.db_echo:
        logging.getLogger("sqlalchemy.engine").setLevel(logging.INFO)

    # aiogram logs every unhandled update at WARNING; keep it but drop the traceback noise
    logging.getLogger("aiogram.dispatcher.dispatcher").setLevel(logging.ERROR)
    _ = json_logs  # reserved for a future structured-logging sink


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


__all__ = ["configure_logging", "get_logger", "FORMAT"]
