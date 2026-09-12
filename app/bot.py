"""Bot + Dispatcher factory: middlewares, routers, commands, error handler."""
from __future__ import annotations

import logging

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import ExceptionTypeFilter
from aiogram.fsm.storage.base import BaseStorage
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, BotCommandScopeDefault, ErrorEvent

from app.config import settings
from app.handlers import routers
from app.middlewares.auth import DatabaseMiddleware, ThrottlingMiddleware, UserContextMiddleware

logger = logging.getLogger(__name__)


def create_bot(token: str | None = None) -> Bot:
    token = token or settings.bot_token
    if not token or "YOUR_BOT_TOKEN" in token:
        raise RuntimeError(
            "BOT_TOKEN is not configured. Copy .env.example to .env and set BOT_TOKEN."
        )
    return Bot(
        token=token,
        default=DefaultBotProperties(
            parse_mode=ParseMode.HTML,
            protect_content=settings.protect_content,
            link_preview_is_disabled=True,
        ),
        **({"base_url": settings.telegram_api_base} if settings.telegram_api_base else {}),
    )


def create_storage() -> BaseStorage:
    """Redis-backed FSM in production, in-memory for single-process dev."""
    url = settings.redis_url.strip() if settings.fsm_storage == "redis" else ""
    if url:
        try:
            from aiogram.fsm.storage.redis import RedisStorage

            storage = RedisStorage.from_url(url)
            logger.info("FSM storage: Redis")
            return storage
        except Exception:  # noqa: BLE001 — fall back rather than crash
            logger.warning("Redis FSM storage unavailable (%s) — using memory", url)
    logger.info("FSM storage: memory (single process only)")
    return MemoryStorage()


def build_dispatcher(bot: Bot) -> Dispatcher:
    dp = Dispatcher(storage=create_storage())
    dp["settings"] = settings

    # Order matters: throttle before touching the database, then the session.
    # UserContextMiddleware is an *inner* middleware on purpose — aiogram only puts
    # the resolved HandlerObject into `data["handler"]` inside trigger(), so an outer
    # middleware would never see which handler is about to run and could not honour
    # the gate exemptions (/start, privacy consent, forced-subscription check).
    for observer in (dp.message, dp.callback_query, dp.my_chat_member,
                     dp.edited_message, dp.inline_query):
        observer.outer_middleware(ThrottlingMiddleware())
        observer.outer_middleware(DatabaseMiddleware())
        observer.middleware(UserContextMiddleware())

    dp.include_routers(*routers)

    dp.errors.register(_on_error, ExceptionTypeFilter(Exception))
    return dp


async def _on_error(event: ErrorEvent) -> None:
    """Never let a handler exception kill the polling loop."""
    logger.exception("unhandled error in %s", event.update.update_id, exc_info=event.exception)
    try:
        from aiogram.types import CallbackQuery, Message

        update = event.update
        target: Message | CallbackQuery | None = update.message or update.callback_query or update.edited_message
        if isinstance(target, CallbackQuery):
            await target.answer("حدث خطأ، جرّب مرة ثانية 🙏")
        elif target is not None:
            await target.answer("حدث خطأ غير متوقع. أعد المحاولة أو أرسل /start.")
    except Exception:  # noqa: BLE001 — best effort
        logger.debug("could not notify user about error")


async def set_commands(bot: Bot) -> None:
    commands_ar = [
        BotCommand(command="start", description="ابدأ / التسجيل"),
        BotCommand(command="menu", description="القائمة الرئيسية"),
        BotCommand(command="help", description="الدليل السريع"),
        BotCommand(command="lang", description="تغيير اللغة AR/EN"),
        BotCommand(command="cancel", description="إلغاء الأمر الحالي"),
        BotCommand(command="id", description="معرّفك لشحن النقاط"),
    ]
    commands_en = [
        BotCommand(command="start", description="Start / sign up"),
        BotCommand(command="menu", description="Main menu"),
        BotCommand(command="help", description="Quick guide"),
        BotCommand(command="lang", description="Switch AR/EN"),
        BotCommand(command="cancel", description="Cancel current step"),
        BotCommand(command="id", description="Your ID for top-ups"),
    ]
    try:
        await bot.set_my_commands(commands_ar, language_code="ar")
        await bot.set_my_commands(commands_en, language_code="en")
        await bot.set_my_commands(commands_ar, scope=BotCommandScopeDefault())
        await bot.set_my_name("الكوتش الذكي 🏋️", language_code="ar")
        await bot.set_my_description(
            "كوتش رياضي وتغذية بالذكاء الاصطناعي: استشارات، متابعة يومية، حساب سعرات أكلك من الصور، "
            "وبرامج تمارين ووجبات.",
            language_code="ar",
        )
        await bot.set_my_description(
            "AI fitness & nutrition coach: consultations, daily tracking, food-photo calorie scanning, "
            "workout and meal programmes.",
            language_code="en",
        )
    except Exception as exc:  # noqa: BLE001 — cosmetic only
        logger.warning("could not set bot commands/description: %s", exc)


__all__ = ["create_bot", "create_storage", "build_dispatcher", "set_commands", "F"]
