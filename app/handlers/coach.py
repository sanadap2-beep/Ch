"""24h personal coach (spec §3.ب) — unlock, live day state, quick actions."""
from __future__ import annotations

import logging
from typing import Any

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.ai.errors import AIBudgetExceeded, AIConfigurationError, AIError
from app.config import settings
from app.db.models import User
from app.db.repositories.economy import DailyLimitReached, InsufficientPoints
from app.handlers.callbacks import CoachCB, MenuCB
from app.handlers.states import CoachFlow
from app.i18n import t
from app.keyboards import coach_menu, remove_keyboard
from app.services.context import Services
from app.services.streak import badge_label
from app.utils.text import escape_html, truncate
from app.utils.tg import answer_long, edit_or_send, send_long, typing

logger = logging.getLogger(__name__)

router = Router(name="coach")

MENU_LABELS = {"🏆 الكوتش الشخصي 24س", "🏆 24h Coach"}
SLOTS_AR = {
    "فطور": "breakfast", "إفطار": "breakfast", "breakfast": "breakfast",
    "غدا": "lunch", "غداء": "lunch", "lunch": "lunch",
    "عشا": "dinner", "عشاء": "dinner", "dinner": "dinner",
    "سناك": "snack", "snack": "snack",
}


def _markup(user: User, lang: str, *, today_done: bool = False):
    return coach_menu(lang, unlocked=user.has_coach_access, balance=int(user.points_balance or 0),
                      today_done=today_done)


# ═══════════════════════════════════════════════════════════════════════════
#  panel
# ═══════════════════════════════════════════════════════════════════════════
async def panel(event: Message | CallbackQuery, user: User, lang: str, services: Services) -> None:
    if not user.onboarding_done:
        await answer_long(event, t(lang, "onb.welcome"))
        return
    if user.has_coach_access:
        log = await services.repos.tracking.daily_log(user, user.local_today())
        status = await services.coach.status_block(user, lang=lang)
        text = t(lang, "coach.intro", status=status)
        markup = _markup(user, lang, today_done=bool(log.workout_done))
    else:
        text = t(lang, "coach.locked", cost=settings.coach_access_cost_points,
                 balance=int(user.points_balance or 0))
        markup = _markup(user, lang)
    if isinstance(event, CallbackQuery):
        await edit_or_send(event, text, reply_markup=markup)
    else:
        await send_long(event.bot, event.chat.id, text, reply_markup=markup)


@router.message(F.text.in_(MENU_LABELS))
async def menu_button(message: Message, user: User, lang: str, services: Services) -> None:
    await panel(message, user, lang, services)


@router.callback_query(MenuCB.filter(F.action == "coach"))
async def menu_callback(callback: CallbackQuery, user: User, lang: str, services: Services) -> None:
    await panel(callback, user, lang, services)


# ═══════════════════════════════════════════════════════════════════════════
#  unlocking (100 points → 30 days)
# ═══════════════════════════════════════════════════════════════════════════
@router.callback_query(CoachCB.filter(F.action == "confirm_unlock"))
async def confirm_unlock(callback: CallbackQuery, user: User, lang: str, services: Services) -> None:
    if user.has_coach_access:
        await panel(callback, user, lang, services)
        return
    cost = settings.coach_access_cost_points
    if int(user.points_balance or 0) < cost:
        await callback.answer(t(lang, "coach.insufficient", need=cost, have=int(user.points_balance or 0)),
                              show_alert=True)
        return
    try:
        await services.points.unlock_coach(user)
    except InsufficientPoints as exc:
        await callback.answer(t(lang, "coach.insufficient", need=exc.required, have=exc.available),
                              show_alert=True)
        return
    until = (
        user.coach_access_until.strftime("%Y-%m-%d")
        if user.coach_access_until
        else ("دائم" if lang.startswith("ar") else "permanent")
    )
    await callback.answer(t(lang, "coach.unlocked", until=until))
    await panel(callback, user, lang, services)


# ═══════════════════════════════════════════════════════════════════════════
#  live day state
# ═══════════════════════════════════════════════════════════════════════════
@router.callback_query(CoachCB.filter(F.action == "status"))
async def status(callback: CallbackQuery, user: User, lang: str, services: Services) -> None:
    log = await services.repos.tracking.daily_log(user, user.local_today())
    trend = await services.repos.tracking.weight_trend(user.id)
    text = services.coach.build_day_context(user, log, trend=trend, lang=lang)
    text += "\n\n" + await services.coach.status_block(user, lang=lang)
    await edit_or_send(callback, text, reply_markup=_markup(user, lang, today_done=bool(log.workout_done)))


# ═══════════════════════════════════════════════════════════════════════════
#  quick actions
# ═══════════════════════════════════════════════════════════════════════════
@router.callback_query(CoachCB.filter(F.action == "workout_done"))
async def workout_done(callback: CallbackQuery, user: User, lang: str, services: Services) -> None:
    session, info = await services.plans.complete_today(user, lang=lang)
    if session is None:
        # no active programme → still log the workout and keep the streak alive
        log = await services.repos.tracking.set_workout_done(user, note="coach button")
        result = await services.streaks.register_activity(user, log)
        info = {"calories": log.burned_calories_est, "duration": None,
                "streak": result.streak, "badges": result.new_badges}
    text = t(lang, "plan.workout_done", minutes=info.get("duration") or "—",
             calories=info.get("calories") or 0, streak=info.get("streak") or 0)
    for badge in info.get("badges") or []:
        text += "\n" + t(lang, "streak.badge", badge=badge_label(badge, lang))
    await edit_or_send(callback, text, reply_markup=_markup(user, lang, today_done=True))


@router.callback_query(CoachCB.filter(F.action == "skip_meal"))
async def skip_meal_start(callback: CallbackQuery, lang: str, state: FSMContext) -> None:
    await state.set_state(CoachFlow.skip_meal)
    await callback.answer()
    ar = lang.startswith("ar")
    await callback.message.answer(
        "أي وجبة فوّت؟ اكتب: فطور / غدا / عشا / سناك — ويفضل تضيف السبب "
        "(مثال: «ما أكلت الغدا، كنت بالدوام»)."
        if ar
        else "Which meal did you skip? Type: breakfast / lunch / dinner / snack — ideally with the reason "
             "(e.g. “skipped lunch, was at work”)."
    )


@router.message(CoachFlow.skip_meal, F.text)
async def skip_meal_answer(
    message: Message, user: User, lang: str, services: Services, state: FSMContext
) -> None:
    await state.clear()
    text = (message.text or "").strip()
    slot = _detect_slot(text)
    day_state = await services.vision.log_skipped_meal(user, slot=slot, reason=truncate(text, 200))

    remaining_meals = max(1, 4 - int(day_state.get("meals_logged") or 0))
    note = ""
    try:
        log = await services.repos.tracking.daily_log(user, user.local_today())
        day_context = services.coach.build_day_context(user, log, lang=lang)
        ar = lang.startswith("ar")
        async with typing(message.bot, message.chat.id):
            outcome = await services.ai.recalc_day(
                user,
                what_happened=(
                    f"فوّت وجبة ({slot or 'غير محددة'}). السبب: {text[:200]}. "
                    f"وزّع المتبقي على {remaining_meals} وجبات من أكلي المحلي وميزانيتي."
                    if ar
                    else f"I skipped a meal ({slot or 'unspecified'}). Reason: {text[:200]}. "
                    f"Spread what's left over {remaining_meals} meals from my local food and budget."
                ),
                day_context=day_context,
                lang=lang,
                remaining_meals=remaining_meals,
            )
        note = _render_recalc(outcome.data or {}, lang)
    except (AIError, AIBudgetExceeded) as exc:
        logger.info("AI recalculation unavailable, falling back to local split: %s", exc)

    if not note:
        note = _local_recalc_text(
            await services.coach.redistribute(user, meals_left=remaining_meals, lang=lang), lang
        )
    await send_long(
        message.bot, message.chat.id, note + "\n\n" + _day_summary(day_state, lang),
        reply_markup=_markup(user, lang),
    )


@router.callback_query(CoachCB.filter(F.action == "meals"))
async def suggest_meals(callback: CallbackQuery, user: User, lang: str, services: Services) -> None:
    await callback.answer()
    log = await services.repos.tracking.daily_log(user, user.local_today())
    day_context = services.coach.build_day_context(user, log, lang=lang)
    try:
        async with typing(callback.bot, callback.message.chat.id):
            outcome = await services.ai.meal_suggestions(user, day_context=day_context, lang=lang)
    except (AIError, AIBudgetExceeded) as exc:
        logger.warning("meal suggestions failed: %s", exc)
        await callback.message.answer(t(lang, "common.ai_busy"))
        return
    data = outcome.data or {}
    text = _render_meal_suggestions(data, lang) or outcome.text or t(lang, "common.error")
    await send_long(callback.bot, callback.message.chat.id, text, reply_markup=_markup(user, lang))


@router.callback_query(CoachCB.filter(F.action == "report"))
async def weekly_report(callback: CallbackQuery, user: User, lang: str, services: Services) -> None:
    await callback.answer()
    try:
        async with typing(callback.bot, callback.message.chat.id):
            text = await services.reports.weekly(user, lang)
    except (AIError, AIBudgetExceeded) as exc:
        logger.warning("weekly report failed: %s", exc)
        await callback.message.answer(t(lang, "common.ai_busy"))
        return
    await send_long(callback.bot, callback.message.chat.id, text, reply_markup=_markup(user, lang))


# ═══════════════════════════════════════════════════════════════════════════
#  free-text coaching (also used by the fallback + voice routers)
# ═══════════════════════════════════════════════════════════════════════════
async def coach_text(
    message: Message, user: User, lang: str, services: Services, *, override_text: str | None = None
) -> bool:
    """Handle a free-text message with the personal coach. Returns True when handled."""
    text_in = override_text if override_text is not None else (message.text or "")
    try:
        async with typing(message.bot, message.chat.id):
            reply = await services.coach.handle_message(user, text_in, lang=lang)
    except DailyLimitReached:
        await message.answer(t(lang, "coach.daily_limit", limit=settings.coach_daily_message_limit))
        return True
    except InsufficientPoints as exc:
        await message.answer(t(lang, "coach.insufficient", need=exc.required, have=exc.available))
        return True
    except AIBudgetExceeded:
        await message.answer(t(lang, "budget.hit"))
        return True
    except AIConfigurationError as exc:
        logger.error("AI misconfigured: %s", exc)
        await message.answer(t(lang, "common.ai_no_key"))
        return True
    except AIError as exc:
        logger.warning("coach AI error: %s", exc)
        await message.answer(t(lang, "common.ai_busy"))
        return True

    text = reply.text
    if reply.charge is not None and reply.charge.points:
        text += "\n\n" + t(lang, "coach.charged", points=abs(reply.charge.points), balance=reply.charge.balance)
    for badge in reply.badges:
        text += "\n" + t(lang, "streak.badge", badge=badge_label(badge, lang))
    log = await services.repos.tracking.daily_log(user, user.local_today())
    await send_long(
        message.bot, message.chat.id, text, disable_web_page_preview=True,
        reply_markup=_markup(user, lang, today_done=bool(log.workout_done)),
    )
    return True


# ═══════════════════════════════════════════════════════════════════════════
#  renderers
# ═══════════════════════════════════════════════════════════════════════════
def _detect_slot(text: str) -> str | None:
    lowered = (text or "").lower()
    for key, value in SLOTS_AR.items():
        if key in lowered:
            return value
    return None


def _meal_line(meal: dict[str, Any], lang: str, index: int | None = None) -> str:
    ar = lang.startswith("ar")
    name = escape_html(str(meal.get("name") or (f"وجبة {index}" if index else "meal")))
    slot = str(meal.get("slot") or "")
    line = f"• <b>{name}</b>"
    if slot:
        line += f" ({escape_html(slot)})" if not ar else f" — {escape_html(slot)}"
    line += (
        f": {int(meal.get('calories') or 0)} سعرة | بروتين {meal.get('protein_g', 0)} جم · "
        f"كارب {meal.get('carbs_g', 0)} جم · دهون {meal.get('fats_g', 0)} جم"
        if ar
        else f": {int(meal.get('calories') or 0)} kcal | P {meal.get('protein_g', 0)} g · "
             f"C {meal.get('carbs_g', 0)} g · F {meal.get('fats_g', 0)} g"
    )
    ingredients = meal.get("ingredients") or []
    if ingredients:
        parts = [f"{i.get('name')} {i.get('grams')}جم" for i in ingredients[:6]]
        line += "\n  " + ("المكونات: " if ar else "Ingredients: ") + ", ".join(map(str, parts))
    method = meal.get("method")
    if method:
        line += f"\n  {truncate(str(method), 220)}"
    swaps = meal.get("swaps") or []
    if swaps:
        line += "\n  " + ("بدائل أرخص: " if ar else "Cheaper swaps: ") + ", ".join(map(str, swaps[:4]))
    return line


def _render_meal_suggestions(data: dict[str, Any], lang: str) -> str:
    ar = lang.startswith("ar")
    meals = data.get("meals") or []
    if not meals:
        return ""
    title = str(data.get("title") or "").strip() or ("وجبات اليوم" if ar else "Today's meals")
    lines = [f"<b>🍽️ {escape_html(title)}</b>"]
    total = int(data.get("daily_calories") or sum(int(m.get("calories") or 0) for m in meals))
    for index, meal in enumerate(meals, start=1):
        lines.append(_meal_line(meal, lang, index))
    lines.append(f"\n🔥 {'الإجمالي' if ar else 'Total'}: ~{total} " + ("سعرة" if ar else "kcal"))
    for note in (data.get("budget_notes") or [])[:3]:
        lines.append(f"💰 {truncate(str(note), 160)}")
    for note in (data.get("supplement_notes") or [])[:2]:
        lines.append(f"💊 {truncate(str(note), 160)}")
    shopping = data.get("shopping_list") or []
    if shopping:
        lines.append("\n<b>" + ("🛒 قائمة السوق" if ar else "🛒 Shopping list") + "</b>")
        for item in shopping[:10]:
            lines.append(f"• {escape_html(str(item.get('item') or ''))} — "
                         f"{escape_html(str(item.get('weekly_amount') or ''))}")
    return truncate("\n".join(lines), 3900)


def _render_recalc(data: dict[str, Any], lang: str) -> str:
    ar = lang.startswith("ar")
    if not data:
        return ""
    parts = [f"🔁 <b>{'أعدت حساب يومك' if ar else 'Day recalculated'}</b>"]
    if data.get("explanation"):
        parts.append(truncate(str(data["explanation"]), 500))
    meals = data.get("suggested_meals") or []
    if meals:
        parts.append("\n".join(_meal_line(meal, lang, i) for i, meal in enumerate(meals, start=1)))
    remaining = data.get("new_remaining_calories")
    if remaining is not None:
        parts.append(
            f"{'المتبقي الجديد' if ar else 'New remaining'}: {int(remaining)} "
            f"{'سعرة' if ar else 'kcal'} | "
            f"{'بروتين' if ar else 'P'} {data.get('new_remaining_protein_g', 0)} جم | "
            f"{'كارب' if ar else 'C'} {data.get('new_remaining_carbs_g', 0)} جم | "
            f"{'دهون' if ar else 'F'} {data.get('new_remaining_fats_g', 0)} جم"
        )
    for warning in (data.get("warnings") or [])[:3]:
        parts.append(f"⚠️ {truncate(str(warning), 200)}")
    return truncate("\n\n".join(p for p in parts if p), 3900)


def _local_recalc_text(meals: list[dict[str, Any]], lang: str) -> str:
    ar = lang.startswith("ar")
    lines = [f"🔁 <b>{'وزّعت المتبقي على وجباتك القادمة' if ar else 'Remaining spread over your next meals'}</b>"]
    for meal in meals:
        lines.append(
            f"• {'وجبة' if ar else 'Meal'} {meal['meal_index']}: {meal['calories']} "
            f"{'سعرة' if ar else 'kcal'} — {'بروتين' if ar else 'P'} {meal['protein_g']} جم | "
            f"{'كارب' if ar else 'C'} {meal['carbs_g']} جم | {'دهون' if ar else 'F'} {meal['fats_g']} جم"
        )
    if ar:
        lines.append("\n💡 فعّل الكوتش الشخصي ليوزّعها على أكل بيتك الحقيقي ويعطيك طريقة التحضير.")
    return "\n".join(lines)


def _day_summary(day_state: dict[str, Any], lang: str) -> str:
    remaining = day_state.get("remaining") or {}
    ar = lang.startswith("ar")
    return (
        f"<b>{'المتبقي اليوم' if ar else 'Left today'}:</b> {day_state.get('remaining_calories', 0)} "
        f"{'سعرة' if ar else 'kcal'} | "
        f"{'بروتين' if ar else 'P'} {remaining.get('protein_g', 0)} "
        f"{'جم' if ar else 'g'} | "
        f"{'كارب' if ar else 'C'} {remaining.get('carbs_g', 0)} | "
        f"{'دهون' if ar else 'F'} {remaining.get('fats_g', 0)} | "
        f"💧 {day_state.get('water_ml', 0)}/{day_state.get('water_target_ml', 0)} "
        f"{'مل' if ar else 'ml'}"
    )


__all__ = ["router", "panel", "coach_text", "remove_keyboard"]
