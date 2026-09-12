"""In-process Telegram stand-in.

``MockedSession`` replaces aiogram's HTTP transport so the *real* dispatcher,
middlewares and handlers can be driven with genuine ``Update`` objects while
every outbound Bot API call is recorded instead of sent. That gives end-to-end
coverage of the handler layer (152 handlers) without a network or a bot token.
"""
from __future__ import annotations

import datetime as dt
from typing import Any
from uuid import uuid4

from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.methods import TelegramMethod
from aiogram.methods.base import TelegramType
from aiogram.types import (
    CallbackQuery,
    Chat,
    ChatMemberAdministrator,
    ChatMemberBanned,
    ChatMemberLeft,
    ChatMemberMember,
    File,
    Message,
    PhotoSize,
    Update,
    Voice,
)
from aiogram.types import User as TgUser


def _jpeg(size: int = 320) -> bytes:
    """A real JPEG so PIL-based optimisation in the media pipeline works."""
    import io

    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (size, size), (180, 140, 90)).save(buffer, format="JPEG", quality=85)
    return buffer.getvalue()


class MockedSession(BaseSession):
    """Records every Bot API call and answers with a valid, minimal object."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[TelegramMethod[Any]] = []
        self.member_status: str = "member"     # for getChatMember (forced-subscription gate)
        self.download_bytes: bytes = _jpeg()
        self.raise_for: dict[str, Exception] = {}   # method api-method name → exception
        self._message_id = 1000

    # ── BaseSession API ──────────────────────────────────────────────────────
    async def close(self) -> None:
        return None

    async def make_request(self, bot: Bot, method: TelegramMethod[TelegramType], timeout: int | None = None) -> Any:
        self.calls.append(method)
        if method.__api_method__ in self.raise_for:
            raise self.raise_for[method.__api_method__]
        return self._answer(bot, method)

    async def stream_content(  # type: ignore[override]
        self,
        url: str,
        headers: dict[str, Any] | None = None,
        timeout: int = 30,
        chunk_size: int = 65536,
        raise_for_status: bool = True,
    ) -> Any:
        yield self.download_bytes

    # ── answers ──────────────────────────────────────────────────────────────
    def _answer(self, bot: Bot, method: TelegramMethod[Any]) -> Any:
        returning = method.__returning__
        name = method.__api_method__

        if name == "getMe":
            return TgUser(id=1, is_bot=True, first_name="الكوتش الذكي", username="ai_coach_bot")
        if name == "getFile":
            return File(file_id="file-1", file_unique_id="u-1", file_path="media/test-file.jpg")
        if name == "getChatMember":
            actor = TgUser(id=7, is_bot=False, first_name="Member")
            if self.member_status == "left":
                return ChatMemberLeft(user=actor, status="left")
            if self.member_status == "kicked":
                return ChatMemberBanned(user=actor, status="kicked")
            if self.member_status == "administrator":
                return ChatMemberAdministrator(user=actor, status="administrator", is_anonymous=False)
            return ChatMemberMember(user=actor, status="member")
        if name in {"sendMessage", "sendPhoto", "sendDocument", "sendVideo", "sendAudio",
                    "editMessageText", "editMessageCaption", "copyMessage", "forwardMessage"}:
            chat_id = getattr(method, "chat_id", 1)
            message = self._message(
                chat_id=chat_id.id if isinstance(chat_id, Chat) else chat_id,
                text=getattr(method, "text", "") or getattr(method, "caption", "") or "",
                reply_markup=getattr(method, "reply_markup", None),
                with_photo=name == "sendPhoto",
            )
            # Telegram responses are bot-mounted, so handlers can chain
            # `status.edit_text()` / `status.delete()` on what they just sent.
            return message.as_(bot)
        if isinstance(returning, type) and issubclass(returning, list):
            return []
        if returning is bool or returning is None:
            return True
        return True

    def _message(self, *, chat_id: Any, text: str, reply_markup: Any = None, with_photo: bool = False) -> Message:
        self._message_id += 1
        # An inbound Message may only carry an InlineKeyboardMarkup; reply keyboards
        # and ReplyKeyboardRemove are valid on outgoing send-methods only.
        from aiogram.types import InlineKeyboardMarkup

        if not isinstance(reply_markup, InlineKeyboardMarkup):
            reply_markup = None
        chat = Chat(id=int(chat_id) if str(chat_id).lstrip("-").isdigit() else 1, type="private",
                    first_name="Tester")
        message = Message(
            message_id=self._message_id,
            date=dt.datetime.now(dt.UTC),
            chat=chat,
            from_user=TgUser(id=1, is_bot=True, first_name="الكوتش الذكي"),
            text=text or None,
            caption=text or None if with_photo else None,
            reply_markup=reply_markup,
        )
        if with_photo:
            message.photo = [
                PhotoSize(file_id="p1", file_unique_id="p1u", width=90, height=90),
                PhotoSize(file_id="p2", file_unique_id="p2u", width=800, height=800),
            ]
        return message

    # ── assertions helpers ───────────────────────────────────────────────────
    def by_name(self, api_method: str) -> list[TelegramMethod[Any]]:
        return [c for c in self.calls if c.__api_method__ == api_method]

    def texts(self, api_method: str = "sendMessage") -> list[str]:
        out: list[str] = []
        for call in self.by_name(api_method):
            value = getattr(call, "text", None) or getattr(call, "caption", None)
            if value:
                out.append(str(value))
        return out

    def all_text(self) -> str:
        return "\n".join(self.texts("sendMessage") + self.texts("sendPhoto")
                         + self.texts("sendDocument") + self.texts("editMessageText"))

    def keyboards(self, api_method: str = "sendMessage") -> list[Any]:
        return [getattr(c, "reply_markup", None) for c in self.by_name(api_method)]

    def alerts(self) -> list[str]:
        """callbackQuery answers that were shown as an alert."""
        return [str(c.text) for c in self.by_name("answerCallbackQuery") if getattr(c, "show_alert", False)]

    def clear(self) -> None:
        self.calls.clear()


def make_bot(session: MockedSession | None = None) -> tuple[Bot, MockedSession]:
    session = session or MockedSession()
    bot = Bot(token="123456789:TEST-token-for-unit-tests", session=session)
    return bot, session


def text_update(
    text: str,
    *,
    tg_id: int = 424242,
    chat_id: int | None = None,
    username: str = "tester",
    first_name: str = "Tester",
    language_code: str = "ar",
    update_id: int | None = None,
) -> Update:
    """A private-chat text message update."""
    chat_id = chat_id or tg_id
    message = Message(
        message_id=1,
        date=dt.datetime.now(dt.UTC),
        chat=Chat(id=chat_id, type="private", first_name=first_name),
        from_user=TgUser(id=tg_id, is_bot=False, first_name=first_name, username=username,
                         language_code=language_code),
        text=text,
    )
    return Update(update_id=update_id or int(uuid4().int % 10**8), message=message)


def callback_update(
    data: str,
    *,
    tg_id: int = 424242,
    chat_id: int | None = None,
    message_text: str = "menu",
    language_code: str = "ar",
    update_id: int | None = None,
) -> Update:
    """An inline-button press on an existing bot message."""
    chat_id = chat_id or tg_id
    origin = Message(
        message_id=9,
        date=dt.datetime.now(dt.UTC),
        chat=Chat(id=chat_id, type="private"),
        from_user=TgUser(id=1, is_bot=True, first_name="bot"),
        text=message_text,
    )
    callback = CallbackQuery(
        id=str(uuid4()),
        from_user=TgUser(id=tg_id, is_bot=False, first_name="Tester", language_code=language_code),
        chat_instance=str(chat_id),
        data=data,
        message=origin,
    )
    return Update(update_id=update_id or int(uuid4().int % 10**8), callback_query=callback)


def photo_update(
    *,
    tg_id: int = 424242,
    caption: str | None = None,
    chat_id: int | None = None,
    language_code: str = "ar",
    update_id: int | None = None,
) -> Update:
    """A photo message (food scan / progress photo)."""
    chat_id = chat_id or tg_id
    message = Message(
        message_id=2,
        date=dt.datetime.now(dt.UTC),
        chat=Chat(id=chat_id, type="private"),
        from_user=TgUser(id=tg_id, is_bot=False, first_name="Tester", language_code=language_code),
        caption=caption,
        photo=[
            PhotoSize(file_id="small", file_unique_id="su", width=90, height=90),
            PhotoSize(file_id="big", file_unique_id="bu", width=1024, height=768, file_size=180_000),
        ],
    )
    return Update(update_id=update_id or int(uuid4().int % 10**8), message=message)


def voice_update(
    *,
    tg_id: int = 424242,
    duration: int = 4,
    chat_id: int | None = None,
    language_code: str = "ar",
    update_id: int | None = None,
) -> Update:
    """A voice note (speech-to-text pipeline)."""
    chat_id = chat_id or tg_id
    message = Message(
        message_id=3,
        date=dt.datetime.now(dt.UTC),
        chat=Chat(id=chat_id, type="private"),
        from_user=TgUser(id=tg_id, is_bot=False, first_name="Tester", language_code=language_code),
        voice=Voice(file_id="voice-1", file_unique_id="vu-1", duration=duration),
    )
    return Update(update_id=update_id or int(uuid4().int % 10**8), message=message)


def build_test_dispatcher(bot: Bot) -> Dispatcher:
    """The production dispatcher (routers + middlewares), wired to the mock bot."""
    from app.bot import build_dispatcher

    return build_dispatcher(bot)


async def feed(dp: Dispatcher, bot: Bot, update: Update) -> Any:
    return await dp.feed_update(bot, update)


__all__ = [
    "MockedSession",
    "make_bot",
    "text_update",
    "callback_update",
    "photo_update",
    "voice_update",
    "build_test_dispatcher",
    "feed",
]
