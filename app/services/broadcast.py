"""Broadcasts to all users (spec §4) with rate-limit handling and progress."""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timedelta
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError

try:  # aiogram >= 3.31 renamed the flood-control exception
    from aiogram.exceptions import TelegramRetryAfter as TelegramRetryAfterError
except ImportError:  # pragma: no cover — older aiogram
    from aiogram.exceptions import TelegramRetryAfterError  # type: ignore[no-redef]
from aiogram.methods.base import TelegramType
from sqlalchemy import select

from app.config import settings
from app.constants import BroadcastStatus
from app.db.base import session_scope
from app.db.models import User, utcnow
from app.db.repositories import Repos

logger = logging.getLogger(__name__)


class BroadcastService:
    """Owns its sessions: a broadcast outlives a single Telegram update."""

    def __init__(self, bot: Bot) -> None:
        self.bot = bot

    # ── creation ─────────────────────────────────────────────────────────────
    async def create(
        self,
        *,
        admin_tg_id: int | None,
        text: str,
        media_file_id: str | None = None,
        audience: str = "all",
        scheduled_for: datetime | None = None,
    ) -> tuple[uuid.UUID, int]:
        async with session_scope() as session:
            repos = Repos(session)
            recipients = await repos.users.broadcast_audience(audience)
            broadcast = await repos.broadcasts.create(
                admin_tg_id,
                text=text,
                media_file_id=media_file_id,
                audience=audience,
                total=len(recipients),
                status=BroadcastStatus.PENDING.value,
                scheduled_for=scheduled_for,
            )
            await repos.audit.log(
                admin_tg_id or 0,
                "broadcast",
                payload={"id": str(broadcast.id), "audience": audience, "total": len(recipients),
                         "chars": len(text)},
            )
            return broadcast.id, len(recipients)

    # ── execution ────────────────────────────────────────────────────────────
    async def run(self, broadcast_id: uuid.UUID | str) -> dict[str, int]:
        if isinstance(broadcast_id, str):
            broadcast_id = uuid.UUID(broadcast_id)
        async with session_scope() as session:
            repos = Repos(session)
            broadcast = await repos.broadcasts.by_id(broadcast_id)
            if broadcast is None:
                return {"sent": 0, "failed": 0}
            broadcast.status = BroadcastStatus.RUNNING.value
            recipients = await repos.users.broadcast_audience(broadcast.audience)
            broadcast.total = len(recipients)
            await session.flush()

        sent = failed = 0
        for start in range(0, len(recipients), settings.broadcast_batch_size):
            batch = recipients[start : start + settings.broadcast_batch_size]
            results = await asyncio.gather(
                *(self._send(tg_id, broadcast.text, broadcast.media_file_id) for tg_id in batch),
                return_exceptions=True,
            )
            for result in results:
                if result is True:
                    sent += 1
                else:
                    failed += 1
            async with session_scope() as session:
                repos = Repos(session)
                broadcast = await repos.broadcasts.by_id(broadcast_id)
                if broadcast:
                    await repos.broadcasts.progress(broadcast, sent, failed)
            await asyncio.sleep(settings.broadcast_delay_ms / 1000.0)

        async with session_scope() as session:
            repos = Repos(session)
            broadcast = await repos.broadcasts.by_id(broadcast_id)
            if broadcast:
                await repos.broadcasts.finish(broadcast, BroadcastStatus.DONE.value)
        logger.info("broadcast %s done: sent=%s failed=%s", broadcast_id, sent, failed)
        return {"sent": sent, "failed": failed}

    async def _send(self, tg_id: int, text: str, media_file_id: str | None) -> bool:
        for attempt in range(3):
            try:
                if media_file_id:
                    await self.bot.send_photo(tg_id, media_file_id, caption=text[:1024])
                else:
                    await self.bot.send_message(tg_id, text, disable_web_page_preview=True)
                return True
            except TelegramRetryAfterError as exc:
                await asyncio.sleep(min(float(exc.retry_after or 5) + 1, 40.0))
            except TelegramForbiddenError:
                logger.info("broadcast: user %s blocked the bot", tg_id)
                return False
            except TelegramAPIError as exc:
                logger.warning("broadcast to %s failed (attempt %s): %s", tg_id, attempt + 1, exc)
                await asyncio.sleep(1.5 * (attempt + 1))
            except Exception as exc:  # noqa: BLE001
                logger.warning("broadcast unexpected error for %s: %s", tg_id, exc)
                return False
        return False

    # ── scheduler hook ───────────────────────────────────────────────────────
    async def run_pending(self) -> int:
        async with session_scope() as session:
            repos = Repos(session)
            pending = await repos.broadcasts.pending()
            ids = [b.id for b in pending]
        for broadcast_id in ids:
            await self.run(broadcast_id)
        return len(ids)

    async def history(self, limit: int = 10) -> list[dict[str, Any]]:
        async with session_scope() as session:
            repos = Repos(session)
            rows = await repos.broadcasts.history(limit=limit)
            return [
                {
                    "id": str(b.id),
                    "text": b.text[:80],
                    "status": b.status,
                    "total": b.total,
                    "sent": b.sent,
                    "failed": b.failed,
                    "created_at": b.created_at.isoformat() if b.created_at else None,
                }
                for b in rows
            ]


async def count_recipients(audience: str = "all") -> int:
    async with session_scope() as session:
        repos = Repos(session)
        return len(await repos.users.broadcast_audience(audience))


def next_slot(delay_minutes: int = 5) -> datetime:
    return utcnow() + timedelta(minutes=delay_minutes)


__all__ = ["BroadcastService", "count_recipients", "next_slot", "select", "User", "TelegramType"]
