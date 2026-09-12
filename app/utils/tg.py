"""Telegram send helpers: chunking, edit-or-send, typing indicator, errors."""
from __future__ import annotations

import logging
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.chat_action import ChatActionSender

from app.constants import TELEGRAM_CHUNK_LIMIT
from app.utils.text import chunk

logger = logging.getLogger(__name__)


async def send_long(bot: Bot, chat_id: int, text: str, **kwargs: Any) -> list[Message]:
    """Send a long HTML message, split on safe boundaries.

    Only the first part carries the reply markup (Telegram would repeat buttons
    on every chunk otherwise).
    """
    parts = chunk(text, TELEGRAM_CHUNK_LIMIT) or ["…"]
    sent: list[Message] = []
    markup = kwargs.pop("reply_markup", None)
    for index, part in enumerate(parts):
        payload = dict(kwargs)
        if index == 0 and markup is not None:
            payload["reply_markup"] = markup
        try:
            sent.append(await bot.send_message(chat_id, part, **payload))
        except TelegramBadRequest as exc:
            # usually an entity/tag problem → resend as plain text
            logger.info("send_long fallback to plain text: %s", exc)
            from app.utils.text import plain

            sent.append(await bot.send_message(chat_id, plain(part)[:TELEGRAM_CHUNK_LIMIT],
                                               reply_markup=payload.get("reply_markup")))
        except TelegramAPIError:
            logger.exception("failed to send message to %s", chat_id)
    return sent


async def answer_long(event: Message | CallbackQuery, text: str, **kwargs: Any) -> list[Message]:
    """Answer whatever update type arrived, with chunking."""
    if isinstance(event, CallbackQuery):
        return await edit_or_send(event, text, **kwargs)
    return await send_long(event.bot, event.chat.id, text, **kwargs)


async def edit_or_send(
    callback: CallbackQuery,
    text: str,
    *,
    reply_markup: InlineKeyboardMarkup | None = None,
    **kwargs: Any,
) -> list[Message]:
    """Edit the current message when the text fits, otherwise send a new one."""
    parts = chunk(text, TELEGRAM_CHUNK_LIMIT) or ["…"]
    await callback.answer()
    # Telegram only accepts an InlineKeyboardMarkup on editMessageText. A reply
    # keyboard (the persistent bottom menu) or a ReplyKeyboardRemove must ride on a
    # freshly *sent* message, so in that case skip the edit entirely — otherwise the
    # call fails client-side validation and the user gets an error instead of a menu.
    editable = reply_markup is None or isinstance(reply_markup, InlineKeyboardMarkup)
    if editable and len(parts) == 1:
        try:
            await callback.message.edit_text(
                parts[0], reply_markup=reply_markup, parse_mode="HTML", **kwargs
            )
            return [callback.message]
        except TelegramBadRequest as exc:
            message = str(exc)
            if "message is not modified" in message:
                return [callback.message]
            if "message can't be edited" in message or "MESSAGE_ID_INVALID" in message:
                pass
            elif "can't parse entities" in message or "unsupported parse_mode" in message:
                from app.utils.text import plain

                await callback.message.answer(plain(parts[0])[:TELEGRAM_CHUNK_LIMIT],
                                              reply_markup=reply_markup)
                return [callback.message]
            else:
                logger.info("edit failed (%s) — sending a new message", exc)
    return await send_long(callback.bot, callback.message.chat.id, text,
                           reply_markup=reply_markup, **kwargs)


async def send_photo_bytes(
    bot: Bot, chat_id: int, data: bytes, caption: str = "", **kwargs: Any
) -> Message:
    from aiogram.types import BufferedInputFile

    return await bot.send_photo(
        chat_id, BufferedInputFile(data, filename="chart.png"),
        caption=caption[:1024] if caption else None, **kwargs
    )


async def send_document_bytes(
    bot: Bot, chat_id: int, data: bytes, filename: str, caption: str = "", **kwargs: Any
) -> Message:
    from aiogram.types import BufferedInputFile

    return await bot.send_document(
        chat_id, BufferedInputFile(data, filename=filename),
        caption=caption[:1024] if caption else None, **kwargs
    )


def typing(bot: Bot, chat_id: int) -> Any:
    """Keeps the "typing…" indicator alive during long AI calls."""
    return ChatActionSender.typing(bot=bot, chat_id=chat_id)


def uploading_photo(bot: Bot, chat_id: int) -> Any:
    return ChatActionSender.upload_photo(bot=bot, chat_id=chat_id)


async def notify(bot: Bot | None, tg_id: int, text: str) -> bool:
    """Fire-and-forget push (used by referral rewards, streaks, jobs)."""
    if bot is None:
        return False
    try:
        await send_long(bot, tg_id, text)
        return True
    except TelegramAPIError as exc:
        logger.info("notify to %s failed: %s", tg_id, exc)
        return False
