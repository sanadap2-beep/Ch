"""Food-photo calorie scanning (spec §3.ج) with the clarification loop."""
from __future__ import annotations

import logging
import re
import uuid
from typing import Any

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.ai.errors import AIBudgetExceeded, AIError
from app.config import settings
from app.constants import MealKind
from app.db.models import User
from app.db.repositories.economy import DailyLimitReached, InsufficientPoints
from app.handlers.callbacks import MenuCB, ScanCB
from app.handlers.states import ScanFlow
from app.i18n import t
from app.keyboards import remove_keyboard, scan_menu
from app.services.context import Services
from app.services.vision import FoodAnalysis, clarification_message, logged_message
from app.utils import validators
from app.utils.text import escape_html
from app.utils.tg import edit_or_send, send_long, typing, uploading_photo

logger = logging.getLogger(__name__)

router = Router(name="scan")

MENU_LABELS = {"🍽️ حساب سعرات الأكل", "🍽️ Food calorie scan"}
ANALYSIS_FIELDS = (
    "meal_name", "items", "calories", "protein_g", "carbs_g", "fats_g", "confidence",
    "assumptions", "clarifications", "is_food", "notes", "model", "cost_usd",
)


# ═══════════════════════════════════════════════════════════════════════════
#  panel
# ═══════════════════════════════════════════════════════════════════════════
async def panel(event: Message | CallbackQuery, user: User, lang: str, services: Services) -> None:
    await services.repos.economy.ensure_windows(user)
    free_left = max(0, settings.food_scan_free_daily - int(user.daily_food_scans_free_used or 0))
    text = t(lang, "scan.intro", free=settings.food_scan_free_daily, cost=settings.food_scan_cost_points)
    markup = scan_menu(lang, free_left=free_left, balance=int(user.points_balance or 0))
    if isinstance(event, CallbackQuery):
        await edit_or_send(event, text, reply_markup=markup)
    else:
        await send_long(event.bot, event.chat.id, text, reply_markup=markup)


@router.message(F.text.in_(MENU_LABELS))
async def menu_button(message: Message, user: User, lang: str, services: Services, state: FSMContext) -> None:
    await state.set_state(ScanFlow.waiting_photo)
    await panel(message, user, lang, services)


@router.callback_query(MenuCB.filter(F.action == "scan"))
async def menu_callback(
    callback: CallbackQuery, user: User, lang: str, services: Services, state: FSMContext
) -> None:
    await state.set_state(ScanFlow.waiting_photo)
    await panel(callback, user, lang, services)


@router.callback_query(ScanCB.filter(F.action == "start"))
async def start_scan(callback: CallbackQuery, lang: str, state: FSMContext) -> None:
    await state.set_state(ScanFlow.waiting_photo)
    await callback.answer(t(lang, "scan.send_photo"))


@router.callback_query(ScanCB.filter(F.action == "today"))
async def today_summary(callback: CallbackQuery, user: User, lang: str, services: Services) -> None:
    log = await services.repos.tracking.daily_log(user, user.local_today())
    meals = await services.repos.tracking.meals_of_day(user.id, user.local_today())
    ar = lang.startswith("ar")
    lines = [
        f"<b>{'📊 يومك حتى الآن' if ar else '📊 Your day so far'}</b>",
        f"• {'السعرات' if ar else 'Calories'}: {int(log.consumed_calories or 0)}/{int(log.target_calories or 0)} "
        f"({'متبقي' if ar else 'left'} <b>{int(log.remaining_calories)}</b>)",
        f"• {'بروتين' if ar else 'Protein'}: {log.consumed_protein_g:g}/{int(log.target_protein_g or 0)} "
        f"{'جم' if ar else 'g'}",
        f"• {'كارب' if ar else 'Carbs'}: {log.consumed_carbs_g:g}/{int(log.target_carbs_g or 0)} | "
        f"{'دهون' if ar else 'Fats'}: {log.consumed_fats_g:g}/{int(log.target_fats_g or 0)}",
        f"• {'ماء' if ar else 'Water'}: {int(log.water_ml or 0)}/{int(log.water_target_ml or 0)} "
        f"{'مل' if ar else 'ml'}",
        f"• {'تمرين' if ar else 'Workout'}: {'✅' if log.workout_done else '❌'} | "
        f"{'وجبات مسجّلة' if ar else 'meals logged'}: {int(log.meals_logged or 0)}",
        f"• {'الالتزام' if ar else 'Adherence'}: {int(log.adherence_score or 0)}%",
    ]
    if meals:
        lines.append(f"\n<b>{'الوجبات' if ar else 'Meals'}</b>")
        for meal in meals:
            lines.append(f"• {escape_html(meal.meal_name or meal.kind)}: {int(meal.calories or 0)} "
                         f"{'سعرة' if ar else 'kcal'}")
    if log.skipped_meals:
        lines.append(f"\n⚠️ {'وجبات مفوّتة' if ar else 'Skipped meals'}: {len(log.skipped_meals)}")
    await edit_or_send(callback, "\n".join(lines),
                       reply_markup=scan_menu(lang, free_left=max(
                           0, settings.food_scan_free_daily - int(user.daily_food_scans_free_used or 0)),
                           balance=int(user.points_balance or 0)))


@router.callback_query(ScanCB.filter(F.action == "manual"))
async def manual_start(callback: CallbackQuery, lang: str, state: FSMContext) -> None:
    await state.set_state(ScanFlow.manual)
    await callback.answer()
    ar = lang.startswith("ar")
    await callback.message.answer(
        "اكتب الوجبة بهذا الشكل:\n<code>شاورما دجاج 650 سعرة بروتين 35</code>\n"
        "أو السعرات فقط: <code>450 سعرة</code>"
        if ar
        else "Type the meal like:\n<code>chicken shawarma 650 kcal protein 35</code>\n"
             "or just calories: <code>450 kcal</code>"
    )


@router.message(ScanFlow.manual, F.text)
async def manual_entry(
    message: Message, user: User, lang: str, services: Services, state: FSMContext
) -> None:
    text = message.text or ""
    calories = _find_calories(text)
    if calories is None:
        ar = lang.startswith("ar")
        await message.answer(
            "ما فهمت السعرات 🤔 اكتبها بهذا الشكل: <code>اسم الوجبة 450 سعرة</code>"
            if ar
            else "I couldn't read the calories 🤔 Format: <code>meal name 450 kcal</code>"
        )
        return
    await state.clear()
    name = _meal_name(text) or ("وجبة" if lang.startswith("ar") else "meal")
    _entry, day_state = await services.vision.log_manual(
        user, name=name, calories=calories,
        protein_g=_find_macro(text, ("بروتين", "protein")) or 0.0,
        carbs_g=_find_macro(text, ("كارب", "كربوهيدرات", "carbs", "carb")) or 0.0,
        fats_g=_find_macro(text, ("دهون", "fats", "fat")) or 0.0,
    )
    await send_long(
        message.bot, message.chat.id,
        f"✅ <b>{escape_html(name)}</b> — {calories} "
        f"{'سعرة' if lang.startswith('ar') else 'kcal'}\n\n{logged_message(day_state, lang)}",
        reply_markup=remove_keyboard(),
    )


# ═══════════════════════════════════════════════════════════════════════════
#  photo analysis
# ═══════════════════════════════════════════════════════════════════════════
@router.message(ScanFlow.waiting_photo, (F.photo | F.document))
async def analyse_photo(
    message: Message, user: User, lang: str, services: Services, state: FSMContext
) -> None:
    downloaded = await services.media.download_photo(message)
    if downloaded is None:
        await message.answer(t(lang, "scan.send_photo"))
        return
    data, mime = downloaded
    file_id = message.photo[-1].file_id if message.photo else (
        message.document.file_id if message.document else None)
    await run_analysis(message, user, lang, services, state,
                       data=data, mime=mime, file_id=file_id, hint=message.caption)


async def run_analysis(
    event: Message,
    user: User,
    lang: str,
    services: Services,
    state: FSMContext,
    *,
    data: bytes,
    mime: str,
    file_id: str | None,
    hint: str | None,
) -> None:
    try:
        async with uploading_photo(event.bot, event.chat.id):
            analysis = await services.vision.analyse_food(
                user, data, mime=mime, telegram_file_id=file_id, hint=hint, lang=lang
            )
    except DailyLimitReached:
        await event.answer(t(lang, "scan.limit", limit=settings.vision_daily_limit))
        return
    except InsufficientPoints as exc:
        await event.answer(t(lang, "coach.insufficient", need=exc.required, have=exc.available))
        return
    except (AIError, AIBudgetExceeded) as exc:
        logger.warning("food analysis failed: %s", exc)
        await event.answer(t(lang, "budget.hit") if isinstance(exc, AIBudgetExceeded)
                           else t(lang, "common.ai_busy"))
        return

    if not analysis.is_food:
        await event.answer(t(lang, "scan.not_food"))
        return

    if analysis.needs_clarification:
        # spec §3.ج — ask instead of guessing
        await state.set_state(ScanFlow.clarification)
        await state.update_data(first_pass=_dump_analysis(analysis), mime=mime, hint=hint or "")
        preliminary = (
            f"\n\n<i>{'تقدير مبدئي' if lang.startswith('ar') else 'Preliminary'}: ~{analysis.calories} "
            f"{'سعرة' if lang.startswith('ar') else 'kcal'}</i>"
        )
        await send_long(event.bot, event.chat.id, clarification_message(analysis, lang) + preliminary)
        return

    await _finish(event, user, lang, services, state, analysis)


@router.message(ScanFlow.clarification, F.text)
async def clarification_answer(
    message: Message, user: User, lang: str, services: Services, state: FSMContext
) -> None:
    data = await state.get_data()
    payload: dict[str, Any] = data.get("first_pass") or {}
    answers = validators.safe_str(message.text, 400)
    if not payload or not answers:
        await state.clear()
        await message.answer(t(lang, "common.error"))
        return

    media_id = payload.get("media_id")
    image: tuple[bytes, str] | None = None
    if media_id:
        media = await services.repos.media.by_id(uuid.UUID(str(media_id)))
        raw = await services.media.load(media) if media else None
        if raw:
            image = (raw, data.get("mime") or "image/jpeg")

    first = _load_analysis(payload)
    try:
        async with typing(message.bot, message.chat.id):
            analysis = await services.vision.finalise_food(
                user, first, answers=answers, image=image, lang=lang, hint=data.get("hint") or None
            )
    except (AIError, AIBudgetExceeded) as exc:
        logger.warning("food re-analysis failed: %s", exc)
        await message.answer(t(lang, "common.ai_busy"))
        return
    await _finish(message, user, lang, services, state, analysis, answers=answers)


async def _finish(
    event: Message,
    user: User,
    lang: str,
    services: Services,
    state: FSMContext,
    analysis: FoodAnalysis,
    *,
    answers: str | None = None,
) -> None:
    await state.clear()
    _entry, day_state = await services.vision.log_meal(
        user, analysis, slot=_slot_from_time(user), kind=MealKind.PHOTO.value,
        description=(answers[:300] if answers else None),
    )
    text = analysis.as_message(lang) + "\n\n" + logged_message(day_state, lang)
    if analysis.charge is not None and analysis.charge.points:
        text += "\n\n" + t(lang, "coach.charged", points=abs(analysis.charge.points),
                           balance=analysis.charge.balance)
    else:
        ar = lang.startswith("ar")
        text += "\n\n🎁 " + ("استخدمت واحدة من صورك المجانية اليوم." if ar
                             else "One of your free daily scans was used.")
    await send_long(event.bot, event.chat.id, text, reply_markup=remove_keyboard())


def _slot_from_time(user: User) -> str:
    hour = user.local_now().hour
    if 4 <= hour < 11:
        return "breakfast"
    if 11 <= hour < 16:
        return "lunch"
    if 16 <= hour < 22:
        return "dinner"
    return "snack"


def _dump_analysis(analysis: FoodAnalysis) -> dict[str, Any]:
    payload: dict[str, Any] = {name: getattr(analysis, name) for name in ANALYSIS_FIELDS}
    payload["media_id"] = str(analysis.media_id) if analysis.media_id else None
    return payload


def _load_analysis(payload: dict[str, Any]) -> FoodAnalysis:
    data = {name: payload.get(name) for name in ANALYSIS_FIELDS}
    data = {k: v for k, v in data.items() if v is not None}
    raw = dict(payload)
    media_id = payload.get("media_id")
    return FoodAnalysis(**data, media_id=uuid.UUID(media_id) if media_id else None, raw=raw)


# ═══════════════════════════════════════════════════════════════════════════
#  manual-entry parsing helpers (Arabic-Indic digits included)
# ═══════════════════════════════════════════════════════════════════════════
_CALORIE_WORDS = r"(?:سعرة|سعرات|سعرات حرارية|كالوري|kcal|calories|calories|cal)?"


def _find_calories(text: str) -> int | None:
    cleaned = validators.normalize_digits(text or "")
    match = re.search(rf"(\d{{2,5}})\s*{_CALORIE_WORDS}", cleaned, re.IGNORECASE)
    if match:
        return int(match.group(1))
    numbers = [int(n) for n in re.findall(r"\d{2,5}", cleaned)]
    return numbers[0] if numbers else None


def _find_macro(text: str, keywords: tuple[str, ...]) -> float | None:
    cleaned = validators.normalize_digits(text or "").lower()
    for key in keywords:
        match = re.search(rf"{re.escape(key)}\D{{0,6}}(\d{{1,4}}(?:[.,]\d)?)", cleaned)
        if match:
            return float(match.group(1).replace(",", "."))
    return None


def _meal_name(text: str) -> str:
    cleaned = re.sub(
        r"\d+(?:[.,]\d+)?\s*(?:سعرة|سعرات|كالوري|kcal|calories|cal|بروتين|protein|كارب|كربوهيدرات|carbs?|دهون|fats?)?",
        " ", text or "", flags=re.IGNORECASE,
    )
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .,-،")
    return cleaned[:80]
