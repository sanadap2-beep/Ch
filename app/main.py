"""Application entrypoint: DB bootstrap, bot, dispatcher, scheduler, polling/webhook."""
from __future__ import annotations

import asyncio
import logging
import sys
from datetime import UTC, datetime

from aiogram import Bot, Dispatcher

from app.ai import close_client
from app.bot import build_dispatcher, create_bot, set_commands
from app.config import settings
from app.db.base import dispose_engine, init_db
from app.jobs import shutdown_scheduler, start_scheduler
from app.state import set_bot
from app.state import state as runtime
from app.utils.logging import configure_logging

logger = logging.getLogger(__name__)

ALLOWED_UPDATES = [
    "message",
    "edited_message",
    "callback_query",
    "inline_query",
    "my_chat_member",
    "chat_member",
]


async def bootstrap() -> tuple[Bot, Dispatcher]:
    """Initialise storage, create the bot and register the dispatcher."""
    await init_db()

    bot = create_bot()
    me = await bot.get_me()
    set_bot(bot, me.username)
    runtime.started_at = datetime.now(UTC)
    logger.info("bot @%s ready (id=%s, env=%s)", me.username, me.id, settings.app_env)

    await set_commands(bot)
    dispatcher = build_dispatcher(bot)
    return bot, dispatcher


async def run_polling(bot: Bot, dispatcher: Dispatcher) -> None:
    logger.info("starting long polling…")
    await dispatcher.start_polling(
        bot,
        allowed_updates=ALLOWED_UPDATES,
        drop_pending_updates=True,
        handle_signals=True,
        polling_timeout=45,
    )


async def run_webhook(bot: Bot, dispatcher: Dispatcher) -> None:
    from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
    from aiohttp import web

    url = f"{settings.webhook_url.rstrip('/')}{settings.webhook_path}"
    await bot.set_webhook(
        url=url,
        secret_token=settings.webhook_secret,
        allowed_updates=ALLOWED_UPDATES,
        drop_pending_updates=True,
        max_connections=40,
    )
    logger.info("webhook set to %s", url)

    app = web.Application()
    SimpleRequestHandler(dispatcher=dispatcher, bot=bot, secret_token=settings.webhook_secret).register(
        app, path=settings.webhook_path
    )
    setup_application(app, dispatcher, bot=bot)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host=settings.webhook_host, port=settings.webhook_port)
    await site.start()
    logger.info("webhook server listening on %s:%s", settings.webhook_host, settings.webhook_port)

    stop_event = asyncio.Event()
    runtime.extra["webhook_stop"] = stop_event
    await stop_event.wait()
    await runner.cleanup()
    await bot.delete_webhook()


async def shutdown(bot: Bot) -> None:
    """Close everything in dependency order (jobs → AI client → bot → DB)."""
    logger.info("shutting down…")
    await shutdown_scheduler()
    await close_client()
    try:
        await bot.session.close()
    except Exception:  # noqa: BLE001 — best effort on exit
        logger.debug("bot session close failed", exc_info=True)
    await dispose_engine()
    logger.info("shutdown complete")


async def main() -> None:
    configure_logging()
    if not settings.has_ai_key:
        logger.warning("NANOGPT_API_KEY is empty — AI features will degrade to offline replies.")
    bot, dispatcher = await bootstrap()
    start_scheduler()
    try:
        if settings.webhook_url:
            await run_webhook(bot, dispatcher)
        else:
            await run_polling(bot, dispatcher)
    except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
        logger.info("interrupt received")
    finally:
        await shutdown(bot)


def cli() -> None:
    """``python -m app.main`` entrypoint."""
    try:
        asyncio.run(main())
    except KeyboardInterrupt:  # pragma: no cover — Ctrl-C on the console
        print("\nstopped", file=sys.stderr)


__all__ = ["main", "cli", "bootstrap", "shutdown", "run_polling", "run_webhook", "ALLOWED_UPDATES"]


if __name__ == "__main__":
    cli()
