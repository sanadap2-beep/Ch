"""Last-resort router: free-text routing, unknown commands, unsupported updates."""
from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from app.db.models import User
from app.handlers.coach import coach_text
from app.handlers.consult import ask as consult_ask
from app.i18n import t
from app.keyboards import remove_keyboard
from app.services.context import Services
from app.utils import validators
from app.utils.tg import send_long

logger = logging.getLogger(__name__)

router = Router(name="fallback")

MAX_QUICK_WEIGHT_LEN = 20


@router.message(F.text.startswith("/"))
async def unknown_command(message: Message, lang: str) -> None:
    ar = lang.startswith("ar")
    await send_long(
        message.bot, message.chat.id,
        ("الأوامر المتاحة:\n/start — التسجيل والقائمة\n/help — الدليل\n/menu — القائمة\n"
         "/lang — تغيير اللغة AR/EN\n/id — معرّفك لشحن النقاط\n/cancel — إلغاء الخطوة الحالية"
         if ar
         else "Available commands:\n/start — sign-up & menu\n/help — guide\n/menu — menu\n"
              "/lang — switch AR/EN\n/id — your ID for top-ups\n/cancel — cancel the current step"),
        reply_markup=remove_keyboard(),
    )


@router.message(F.text)
async def free_text(
    message: Message, user: User, lang: str, services: Services, state: FSMContext
) -> None:
    """Route typed text: coach (when unlocked) → general consult, plus free shortcuts."""
    if not user.onboarding_done:
        await message.answer(t(lang, "onb.welcome"), reply_markup=remove_keyboard())
        return

    text = (message.text or "").strip()
    if not text:
        return

    if user.has_coach_access:
        await coach_text(message, user, lang, services)
        return

    # free shortcuts that need no AI spend
    ar = lang.startswith("ar")
    if len(text) <= MAX_QUICK_WEIGHT_LEN:
        weight = validators.parse_weight(text)
        if weight is not None and _mentions_weight(text, ar):
            await send_long(message.bot, message.chat.id,
                            await services.progress.log_weight(user, weight, lang=lang))
            return
    water = validators.parse_water(text)
    if water:
        log = await services.repos.tracking.add_water(user, user.local_today(), water)
        await message.answer(
            f"💧 {'سجّلت' if ar else 'Logged'} {water} "
            f"{'مل' if ar else 'ml'} — {int(log.water_ml or 0)}/{int(log.water_target_ml or 0)} "
            f"{'مل اليوم' if ar else 'ml today'}."
        )
        return
    if validators.is_skip(text):
        await services.vision.log_skipped_meal(user, slot=None, reason=validators.safe_str(text, 120))
        await message.answer(
            "⚠️ سجّلت تخطّي الوجبة. لإعادة توزيع يومك تلقائيًا على وجباتك القادمة فعّل الكوتش الشخصي 🏆"
            if ar
            else "⚠️ Skipped meal logged. Automatic redistribution of your day needs the personal coach 🏆"
        )
        return

    await consult_ask(message, text, user=user, lang=lang, services=services, state=state)


def _mentions_weight(text: str, ar: bool) -> bool:
    keys = ("وزن", "وزني", "kilos", "kg", "weight") if not ar else ("وزن", "وزني", "كجم", "كيلو")
    lowered = text.lower()
    return any(key in lowered for key in keys)


@router.message(F.sticker)
async def sticker(message: Message, lang: str) -> None:
    ar = lang.startswith("ar")
    await message.answer("😄 " + ("أرسل سؤالك أو صورة أكلك." if ar else "Send your question or a food photo."))


@router.message()
async def anything_else(message: Message, lang: str) -> None:
    ar = lang.startswith("ar")
    await message.answer(
        "ما أقدر أتعامل مع هذا النوع من الرسائل 🤔 اكتب سؤالك أو أرسل صورة وجبتك أو ملاحظة صوتية."
        if ar
        else "I can't handle that message type 🤔 Type a question, or send a food photo or a voice note."
    )
