"""Request middlewares: DB session, user context, gates (ban / channel / consent).

Order matters and is registered in :func:`app.bot.register_middlewares`:

1. ``DatabaseMiddleware`` (outer, every update) — one session per update,
   commit on success / rollback on error, and a ready ``Services`` container.
2. ``ThrottlingMiddleware`` — cheap in-memory rate limit.
3. ``UserContextMiddleware`` (inner, Message/CallbackQuery) — resolves the user,
   enforces ban → forced channel subscription → privacy consent, and injects
   ``user``/``lang``/``services`` into the handler.
"""
from __future__ import annotations

import logging
import time
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware, Bot
from aiogram.dispatcher.event.handler import HandlerObject
from aiogram.types import CallbackQuery, Message, TelegramObject
from aiogram.types import User as TgUser

from app.config import settings
from app.db.base import session_scope
from app.db.repositories import Repos
from app.i18n import t
from app.keyboards import forced_subscription, privacy_consent
from app.services.context import Services
from app.services.privacy import PrivacyService

logger = logging.getLogger(__name__)

Handler = Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]]

# update types that must stay reachable even when a gate is active
GATE_EXEMPT_HANDLERS = frozenset(
    {
        "cmd_start",
        "forced_check",
        "privacy_accept",
        "privacy_decline",
        "privacy_policy",
    }
)


class DatabaseMiddleware(BaseMiddleware):
    """One DB session + service container per update."""

    async def __call__(self, handler: Handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        bot: Bot | None = data.get("bot")
        async with session_scope() as session:
            data["session"] = session
            data["services"] = Services(session, bot)
            try:
                return await handler(event, data)
            finally:
                services: Services | None = data.get("services")
                if services is not None:
                    await services.close()


class ThrottlingMiddleware(BaseMiddleware):
    """Per-user rate limit (in-memory; swap for Redis when running multi-process)."""

    def __init__(self, limit: int = 8, window: float = 6.0) -> None:
        self.limit = limit
        self.window = window
        self._hits: dict[int, deque[float]] = defaultdict(deque)

    async def __call__(self, handler: Handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        from_user: TgUser | None = getattr(event, "from_user", None)
        if from_user is None:
            return await handler(event, data)
        now = time.monotonic()
        hits = self._hits[from_user.id]
        while hits and now - hits[0] > self.window:
            hits.popleft()
        if len(hits) >= self.limit:
            if isinstance(event, CallbackQuery):
                await event.answer(t(data.get("lang", "ar"), "throttle", seconds=int(self.window)),
                                   show_alert=True)
            elif isinstance(event, Message):
                await event.answer(t(data.get("lang", "ar"), "throttle", seconds=int(self.window)))
            return None
        hits.append(now)
        return await handler(event, data)


class UserContextMiddleware(BaseMiddleware):
    """Resolve the user and enforce the gates."""

    def __init__(self, forced_channels: list[str] | None = None) -> None:
        self.forced_channels = forced_channels or []

    async def __call__(self, handler: Handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        from_user: TgUser | None = getattr(event, "from_user", None)
        services: Services = data["services"]
        if from_user is None:
            # channel posts / inline queries without a user: let specialised handlers decide
            data.setdefault("user", None)
            data.setdefault("lang", "ar")
            return await handler(event, data)

        repos = services.repos
        user, created = await repos.users.get_or_create(
            from_user.id,
            defaults={
                "username": from_user.username,
                "first_name": from_user.first_name,
                "last_name": from_user.last_name,
                "language_code": _lang_of(from_user),
            },
        )
        await repos.users.sync_telegram_profile(
            user,
            {
                "username": from_user.username,
                "first_name": from_user.first_name,
                "last_name": from_user.last_name,
                "language_code": from_user.language_code,
            },
        )
        if created and user.language_code in (None, ""):
            user.language_code = _lang_of(from_user)
        await repos.users.touch(user)
        await repos.session.flush()

        lang = user.language_code or "ar"
        data["user"] = user
        data["lang"] = lang
        data["is_new_user"] = created
        data["is_admin"] = from_user.id in settings.admin_ids

        exempt = _is_exempt(event, data)

        # ── gate 1: ban ──────────────────────────────────────────────────────
        if user.is_banned and not exempt:
            await _reply_gate(
                event,
                t(lang, "banned.body",
                  reason=f"\nالسبب: {user.ban_reason}" if user.ban_reason else "",
                  contact=f"@{settings.support_username.lstrip('@')}" if settings.support_username else "الإدارة"),
                lang,
            )
            return None

        # ── gate 2: forced channel subscription ──────────────────────────────
        channels = await _forced_channels(services)
        if channels and not exempt:
            subscribed = await _check_channels(data.get("bot"), from_user.id, channels)
            if not subscribed:
                text = t(lang, "forced.body", channels="\n".join(f"• {c}" for c in channels))
                if isinstance(event, CallbackQuery):
                    await event.answer(text, show_alert=True)
                else:
                    await event.answer(text, reply_markup=forced_subscription(channels, lang))
                return None

        # ── gate 3: privacy consent ──────────────────────────────────────────
        privacy = PrivacyService(repos)
        if privacy.needs_consent(user) and not exempt:
            text = t(lang, "privacy.ask") + "\n\n" + t(lang, "privacy.body", retention=settings.keep_food_photos_days)
            if isinstance(event, CallbackQuery):
                await event.answer(t(lang, "privacy.ask"), show_alert=True)
            else:
                await event.answer(text, reply_markup=privacy_consent(lang), disable_web_page_preview=True)
            return None

        return await handler(event, data)


# ═══════════════════════════════════════════════════════════════════════════
#  helpers
# ═══════════════════════════════════════════════════════════════════════════
# Callback prefixes that must stay reachable while a gate is active, so the user
# can always satisfy the gate instead of being locked out of the bot.
GATE_EXEMPT_CALLBACK_PREFIXES = ("prv:", "fsub:")


def _is_exempt(event: TelegramObject, data: dict[str, Any]) -> bool:
    """True when this update must bypass the ban/channel/consent gates.

    Two independent checks, because either can be unavailable:

    * the resolved handler name (only present when this middleware runs as an
      *inner* middleware — aiogram sets ``data["handler"]`` inside ``trigger()``);
    * the shape of the update itself (``/start`` and the consent/subscription
      buttons), which works no matter where the middleware is mounted.
    """
    handler_name = getattr(getattr(data.get("handler"), "callback", None), "__name__", "") or ""
    if handler_name in GATE_EXEMPT_HANDLERS:
        return True

    if isinstance(event, CallbackQuery):
        payload = str(event.data or "")
        return payload.startswith(GATE_EXEMPT_CALLBACK_PREFIXES)

    if isinstance(event, Message):
        text = (event.text or "").strip()
        return text.startswith("/start")

    return False


def _lang_of(from_user: TgUser) -> str:
    code = (from_user.language_code or "ar").lower()
    return "en" if code.startswith("en") else "ar"


async def _forced_channels(services: Services) -> list[str]:
    """Live list: admin panel override first, then settings."""
    from app.db.repositories.ops import SettingsRepository

    repo = SettingsRepository(services.session)
    override = await repo.get("forced_channels")
    enabled = await repo.get("forced_channels_enabled")
    if isinstance(override, list) and override:
        if enabled is False:
            return []
        return [str(c) for c in override]
    if enabled is not None and not enabled:
        return []
    return list(settings.forced_channels) if settings.forced_channels_enabled else []


async def _check_channels(bot: Bot | None, tg_id: int, channels: list[str]) -> bool:
    if bot is None or not channels:
        return True
    for channel in channels:
        chat_id: Any = channel
        if channel.startswith("@"):
            chat_id = channel
        elif channel.lstrip("-").isdigit():
            chat_id = int(channel)
        try:
            member = await bot.get_chat_member(chat_id, tg_id)
        except Exception as exc:  # noqa: BLE001 — bot not admin / channel gone → don't lock users out
            logger.info("forced-subscription check failed for %s: %s", channel, exc)
            continue
        if member.status in {"left", "kicked"}:
            return False
    return True


async def _reply_gate(event: TelegramObject, text: str, lang: str) -> None:
    if isinstance(event, CallbackQuery):
        await event.answer(text, show_alert=True)
    elif isinstance(event, Message):
        try:
            await event.answer(text, disable_web_page_preview=True)
        except Exception:  # noqa: BLE001
            logger.debug("could not send gate message", exc_info=True)


async def is_admin_user(repos: Repos, tg_id: int) -> bool:
    return tg_id in settings.admin_ids


__all__ = [
    "DatabaseMiddleware",
    "ThrottlingMiddleware",
    "UserContextMiddleware",
    "GATE_EXEMPT_HANDLERS",
    "GATE_EXEMPT_CALLBACK_PREFIXES",
    "HandlerObject",
]
