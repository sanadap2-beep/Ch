"""Progress tracking UI: weight, tape measurements, photos, charts, reports, export."""
from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.ai.errors import AIBudgetExceeded, AIError
from app.config import settings
from app.constants import PointsReason
from app.db.models import User
from app.db.repositories.economy import DailyLimitReached, InsufficientPoints
from app.handlers.callbacks import MenuCB, ProgressCB
from app.handlers.states import ProgressFlow
from app.i18n import t
from app.keyboards import photo_angles, progress_menu, remove_keyboard
from app.services.context import Services
from app.utils import validators
from app.utils.tg import (
    edit_or_send,
    send_document_bytes,
    send_long,
    send_photo_bytes,
    typing,
)

logger = logging.getLogger(__name__)

router = Router(name="progress")

MENU_LABELS = {"📈 تقدمي", "📈 My progress"}


async def open_panel(event: Message | CallbackQuery, user: User, lang: str, services: Services) -> None:
    text = await services.progress.overview(user, lang=lang)
    if isinstance(event, CallbackQuery):
        await edit_or_send(event, text, reply_markup=progress_menu(lang))
    else:
        await send_long(event.bot, event.chat.id, text, reply_markup=progress_menu(lang))


@router.message(F.text.in_(MENU_LABELS))
async def menu_button(message: Message, user: User, lang: str, services: Services) -> None:
    if not user.onboarding_done:
        await message.answer(t(lang, "onb.welcome"))
        return
    await open_panel(message, user, lang, services)


@router.callback_query(MenuCB.filter(F.action == "progress"))
async def menu_callback(callback: CallbackQuery, user: User, lang: str, services: Services) -> None:
    await open_panel(callback, user, lang, services)


# ═══════════════════════════════════════════════════════════════════════════
#  weight
# ═══════════════════════════════════════════════════════════════════════════
@router.callback_query(ProgressCB.filter(F.action == "weight"))
async def ask_weight(callback: CallbackQuery, lang: str, state: FSMContext) -> None:
    await state.set_state(ProgressFlow.weight)
    await callback.answer()
    await callback.message.answer(t(lang, "prog.ask_weight"), reply_markup=remove_keyboard())


@router.message(ProgressFlow.weight, F.text)
async def save_weight(
    message: Message, user: User, lang: str, services: Services, state: FSMContext
) -> None:
    weight = validators.parse_weight(message.text)
    if weight is None:
        await message.answer(t(lang, "onb.invalid_number", example="81.4"))
        return
    await state.clear()
    text = await services.progress.log_weight(user, weight, lang=lang)
    await send_long(message.bot, message.chat.id, text, reply_markup=progress_menu(lang))


# ═══════════════════════════════════════════════════════════════════════════
#  body measurements (spec §5.4)
# ═══════════════════════════════════════════════════════════════════════════
@router.callback_query(ProgressCB.filter(F.action == "measurements"))
async def ask_measurements(callback: CallbackQuery, lang: str, state: FSMContext) -> None:
    await state.set_state(ProgressFlow.measurements)
    await callback.answer()
    await callback.message.answer(t(lang, "prog.ask_measurements"), reply_markup=remove_keyboard())


@router.message(ProgressFlow.measurements, F.text)
async def save_measurements(
    message: Message, user: User, lang: str, services: Services, state: FSMContext
) -> None:
    values = validators.parse_measurements(message.text)
    if not values:
        await message.answer(t(lang, "prog.ask_measurements"))
        return
    await state.clear()
    text = await services.progress.log_measurements(user, values, lang=lang)
    await send_long(message.bot, message.chat.id, text, reply_markup=progress_menu(lang))


# ═══════════════════════════════════════════════════════════════════════════
#  progress photos
# ═══════════════════════════════════════════════════════════════════════════
@router.callback_query(ProgressCB.filter(F.action == "photo"))
async def ask_photo(callback: CallbackQuery, lang: str, state: FSMContext) -> None:
    await state.set_state(ProgressFlow.photo_angle)
    await callback.answer()
    await callback.message.answer(t(lang, "prog.ask_photo") + "\n" + t(lang, "prog.photo_angle"),
                                  reply_markup=photo_angles(lang))


@router.callback_query(ProgressCB.filter(F.action == "angle"))
async def choose_angle(callback: CallbackQuery, cb: ProgressCB, lang: str, state: FSMContext) -> None:
    await state.set_state(ProgressFlow.waiting_photo)
    await state.update_data(angle=cb.arg)
    await callback.answer()
    await callback.message.answer(t(lang, "prog.ask_photo"), reply_markup=remove_keyboard())


@router.message(ProgressFlow.waiting_photo, (F.photo | F.document))
async def analyse_progress_photo(
    message: Message, user: User, lang: str, services: Services, state: FSMContext
) -> None:
    data = await state.get_data()
    angle = data.get("angle") or "front"
    downloaded = await services.media.download_photo(message)
    if downloaded is None:
        await message.answer(t(lang, "prog.ask_photo"))
        return
    raw, mime = downloaded
    file_id = message.photo[-1].file_id if message.photo else (
        message.document.file_id if message.document else None)
    await state.clear()

    try:
        async with typing(message.bot, message.chat.id):
            result = await services.vision.analyse_progress_photo(
                user, raw, mime=mime, angle=angle, caption=message.caption,
                telegram_file_id=file_id, lang=lang,
            )
    except DailyLimitReached:
        await message.answer(t(lang, "scan.limit", limit=settings.vision_daily_limit))
        return
    except InsufficientPoints as exc:
        await message.answer(t(lang, "coach.insufficient", need=exc.required, have=exc.available))
        return
    except (AIError, AIBudgetExceeded) as exc:
        logger.warning("progress photo analysis failed: %s", exc)
        await message.answer(t(lang, "common.ai_busy"))
        return

    text = t(lang, "prog.photo_saved", angle=angle) + "\n\n" + result.text
    if result.charge is not None and result.charge.points:
        text += "\n\n" + t(lang, "coach.charged", points=abs(result.charge.points),
                           balance=result.charge.balance)
    await send_long(message.bot, message.chat.id, text, reply_markup=progress_menu(lang))


# ═══════════════════════════════════════════════════════════════════════════
#  charts, reports, PDF export
# ═══════════════════════════════════════════════════════════════════════════
@router.callback_query(ProgressCB.filter(F.action == "chart"))
async def chart(callback: CallbackQuery, user: User, lang: str, services: Services) -> None:
    await callback.answer()
    png = await services.progress.combined_chart(user, lang=lang)
    if not png:
        await callback.message.answer(t(lang, "prog.no_data"))
        return
    await send_photo_bytes(callback.bot, callback.message.chat.id, png, caption=t(lang, "prog.chart"))


@router.callback_query(ProgressCB.filter(F.action.in_({"report_week", "report_month"})))
async def report(callback: CallbackQuery, cb: ProgressCB, user: User, lang: str, services: Services) -> None:
    await callback.answer()
    period = "month" if cb.action == "report_month" else "week"
    try:
        async with typing(callback.bot, callback.message.chat.id):
            text = await services.reports.periodic(user, period=period, lang=lang)
    except (AIError, AIBudgetExceeded) as exc:
        logger.warning("report failed: %s", exc)
        await callback.message.answer(t(lang, "common.ai_busy"))
        return
    await send_long(callback.bot, callback.message.chat.id, text, reply_markup=progress_menu(lang))


@router.callback_query(ProgressCB.filter(F.action == "export"))
async def export_pdf(callback: CallbackQuery, user: User, lang: str, services: Services) -> None:
    await callback.answer()
    cost = settings.report_export_cost_points or settings.data_export_cost_points
    if cost:
        try:
            await services.points.charge_flat(user, cost, PointsReason.REPORT_EXPORT, note="PDF export")
        except InsufficientPoints as exc:
            await callback.answer(t(lang, "coach.insufficient", need=exc.required, have=exc.available),
                                  show_alert=True)
            return
    ar = lang.startswith("ar")
    status = await callback.message.answer("⏳ " + ("أجهّز ملف رحلتك…" if ar else "Building your journey file…"))
    try:
        pdf = await services.reports.export_pdf(user, lang=lang, include_charts=True)
    except Exception:  # noqa: BLE001 — PDF generation must never crash the chat
        logger.exception("PDF export failed")
        await status.edit_text(t(lang, "common.error"))
        return
    await status.delete()
    await send_document_bytes(
        callback.bot, callback.message.chat.id, pdf,
        filename=f"coach-journey-{user.tg_id}.pdf",
        caption=("📄 تقرير رحلتك الكامل" if ar else "📄 Your full journey report"),
    )


@router.callback_query(ProgressCB.filter(F.action == "menu"))
async def back_to_menu(callback: CallbackQuery, user: User, lang: str, services: Services) -> None:
    await open_panel(callback, user, lang, services)
