"""Process-wide runtime singletons (bot, scheduler, shared AI client).

Handlers get these from the aiogram ``Bot``/middleware injection, but a few
background jobs (scheduler) need access outside a request context.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from aiogram import Bot


@dataclass
class RuntimeState:
    bot: Bot | None = None
    bot_username: str | None = None
    scheduler: Any | None = None
    ai_client: Any | None = None
    started_at: Any | None = None
    extra: dict[str, Any] = field(default_factory=dict)


state = RuntimeState()


def set_bot(bot: Bot, username: str | None = None) -> None:
    state.bot = bot
    state.bot_username = username or getattr(getattr(bot, "me", None), "username", None)


def referral_base_link() -> str:
    """``https://t.me/<bot>`` — used to build referral deep links."""
    username = state.bot_username
    return f"https://t.me/{username}" if username else "https://t.me/your_bot"
