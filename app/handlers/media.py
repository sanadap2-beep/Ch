"""Global media handling: voice notes → text, and food photos sent outside a flow."""
from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from app.ai.errors import AIBudgetExceeded, AIError
from app.config import settings
from app.constants import PointsReason
from app.db.models import User
from app.db.repositories.economy import InsufficientPoints
from app.handlers.coach import coach_text
from app.handlers.consult import ask as consult_ask
from app.handlers.scan import run_analysis
from app.i18n import t
from app.services.context import Services
from app.utils.text import escape_html
from app.utils.tg import send_long, typing

logger = logging.getLogger(__name__)

router = Router(name="media")


# ═══════════════════════════════════════════════════════════════════════════
#  voice notes (spec extra §8 — talk instead of typing)
# ═══════════════════════════════════════════════════════════════════════════
@router.message(F.voice)
async def voice_note(
    message: Message, user: User, lang: str, services: Services, state: FSMContext
) -> None:
    ar = lang.startswith("ar")
    if not settings.voice_enabled:
        await message.answer(t(lang, "voice.failed"))
        return
    if message.voice.duration and message.voice.duration > settings.voice_max_seconds:
        await message.answer(
            f"⏱️ {'أقصر من فضلك' if ar else 'Shorter please'} — "
            f"{settings.voice_max_seconds} "
            f"{'ثانية كحد أقصى' if ar else 'seconds max'}."
        )
        return

    status = await message.answer("🎙️ " + ("أفرّغ الصوت…" if ar else "Transcribing…"))
    try:
        async with typing(message.bot, message.chat.id):
            downloaded = await services.media.download_voice(message)
            if downloaded is None:
                raise AIError("could not download the voice note")
            audio, _mime = downloaded
            transcript, _usage, _model = await services.ai.transcribe(
                audio, filename="voice.ogg", lang="ar" if ar else "en", user_id=user.id
            )
    except (AIError, AIBudgetExceeded) as exc:
        logger.warning("transcription failed: %s", exc)
        await status.edit_text(t(lang, "voice.failed"))
        return
    await _delete_quietly(status)

    text = (transcript or "").strip()
    if not text:
        await message.answer(t(lang, "voice.failed"))
        return

    if settings.voice_charge_points:
        try:
            await services.points.charge_flat(
                user, settings.voice_charge_points, PointsReason.ADJUSTMENT, note="voice → text"
            )
        except InsufficientPoints as exc:
            await message.answer(t(lang, "coach.insufficient", need=exc.required, have=exc.available))
            return

    # echo the transcription so the user can see what was understood
    await message.answer(t(lang, "voice.transcribed", text=escape_html(text[:400])))

    if not user.onboarding_done:
        await message.answer(t(lang, "onb.welcome"))
        return
    if user.has_coach_access:
        await coach_text(message, user, lang, services, override_text=text)
    else:
        await consult_ask(message, text, user=user, lang=lang, services=services, state=state)


# ═══════════════════════════════════════════════════════════════════════════
#  photos sent outside a flow → food analysis (spec §3.ج)
# ═══════════════════════════════════════════════════════════════════════════
@router.message(F.photo)
async def loose_photo(
    message: Message, user: User, lang: str, services: Services, state: FSMContext
) -> None:
    ar = lang.startswith("ar")
    if not user.onboarding_done:
        await message.answer(t(lang, "onb.welcome"))
        return

    caption = message.caption
    if caption and _is_progress_request(caption):
        await message.answer(
            "📸 صور تقدم الجسم تُرسل من: 📈 تقدمي ← 📷 صورة تقدم (حتى أقارنها بصورك السابقة بأمان)."
            if ar
            else "📸 Body-progress photos go through: 📈 My progress ← 📷 Progress photo "
                 "(so I can compare them with your previous ones safely)."
        )
        return

    downloaded = await services.media.download_photo(message)
    if downloaded is None:
        await message.answer(t(lang, "scan.send_photo"))
        return
    data, mime = downloaded
    await run_analysis(
        message, user, lang, services, state,
        data=data, mime=mime, file_id=message.photo[-1].file_id, hint=caption,
    )


@router.message(F.video | F.video_note | F.audio | F.document)
async def unsupported_media(message: Message, lang: str) -> None:
    ar = lang.startswith("ar")
    await message.answer(
        "أقدر أحلل <b>صور الأكل</b> و<b>الملاحظات الصوتية</b> فقط. أرسل صورة وجبتك أو اسألني نصًا/صوتًا."
        if ar
        else "I can analyse <b>food photos</b> and <b>voice notes</b> only. Send a meal photo or ask by text/voice."
    )


def _is_progress_request(caption: str) -> bool:
    lowered = (caption or "").lower()
    keys = ("تقدم", "جسمي", "progress", "before", "after", "بطن", "abs", "مقارنة")
    return any(key in lowered for key in keys)


async def _delete_quietly(message: Message) -> None:
    try:
        await message.delete()
    except Exception:  # noqa: BLE001 — best effort
        logger.debug("could not delete the transcription notice")


__all__ = ["router", "send_long"]
