"""Programme generation & daily sessions (training / nutrition / boxing / mobility)."""
from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.ai.errors import AIBudgetExceeded, AIError
from app.db.models import User
from app.handlers.callbacks import MenuCB, PlanCB
from app.handlers.states import PlanFlow
from app.i18n import t
from app.keyboards import plan_menu, remove_keyboard
from app.services.context import Services
from app.services.plans import boxing_next_phase
from app.services.streak import badge_label
from app.utils.text import truncate
from app.utils.tg import answer_long, edit_or_send, send_long, typing

logger = logging.getLogger(__name__)

router = Router(name="plans")

MENU_LABELS = {"📋 برنامجي", "📋 My programme"}
VALID_KINDS = {"training", "nutrition", "boxing", "flexibility"}


async def panel(event: Message | CallbackQuery, user: User, lang: str, services: Services) -> None:
    plan = await services.repos.plans.any_active_plan(user.id)
    text = await services.plans.active_plan_summary(user, lang=lang)
    markup = plan_menu(lang, has_plan=plan is not None, kind=plan.kind if plan else None)
    if isinstance(event, CallbackQuery):
        await edit_or_send(event, text, reply_markup=markup)
    else:
        await send_long(event.bot, event.chat.id, text, reply_markup=markup)


@router.message(F.text.in_(MENU_LABELS))
async def menu_button(message: Message, user: User, lang: str, services: Services) -> None:
    if not user.onboarding_done:
        await message.answer(t(lang, "onb.welcome"))
        return
    await panel(message, user, lang, services)


@router.callback_query(MenuCB.filter(F.action == "plan"))
async def menu_callback(callback: CallbackQuery, user: User, lang: str, services: Services) -> None:
    await panel(callback, user, lang, services)


# ═══════════════════════════════════════════════════════════════════════════
#  generation
# ═══════════════════════════════════════════════════════════════════════════
@router.callback_query(PlanCB.filter(F.action == "generate"))
async def generate(
    callback: CallbackQuery, cb: PlanCB, user: User, lang: str, services: Services, state: FSMContext
) -> None:
    kind = cb.kind if cb.kind in VALID_KINDS else "training"
    await callback.answer(t(lang, "plan.generating"))
    if not user.onboarding_done:
        await callback.message.answer(t(lang, "onb.welcome"), reply_markup=remove_keyboard())
        return
    await state.clear()

    notice = await callback.message.answer(t(lang, "plan.generating"))
    try:
        async with typing(callback.bot, callback.message.chat.id):
            result = await services.plans.generate(user, kind, lang=lang)
    except (AIError, AIBudgetExceeded) as exc:
        logger.warning("plan generation failed: %s", exc)
        await notice.edit_text(t(lang, "budget.hit") if isinstance(exc, AIBudgetExceeded)
                               else t(lang, "common.ai_busy"))
        return
    await _delete_quietly(notice)

    if result.plan is None:
        await send_long(callback.bot, callback.message.chat.id, result.text or t(lang, "common.error"))
        return

    weeks = len(result.plan.weeks or [])
    days = sum(len(w.get("days") or []) for w in (result.plan.weeks or []))
    header = t(lang, "plan.saved", title=result.plan.title, weeks=weeks, days=days)
    notes = list(result.warnings or [])
    notes += [str(n) for n in (result.data.get("safety_notes") or [])]
    if notes:
        header += "\n\n" + t(lang, "plan.safety", notes="\n".join(f"• {truncate(n, 200)}" for n in notes[:4]))
    await send_long(callback.bot, callback.message.chat.id, header)
    await send_long(callback.bot, callback.message.chat.id, result.text,
                    reply_markup=plan_menu(lang, has_plan=True, kind=result.plan.kind))


@router.callback_query(PlanCB.filter(F.action == "next_phase"))
async def next_phase(callback: CallbackQuery, user: User, lang: str, services: Services) -> None:
    await callback.answer()
    current = await services.repos.plans.active_plan(user.id, kind="boxing")
    week = 2
    if current and current.weeks:
        week = int(current.weeks[-1].get("phase") or current.weeks[-1].get("week_index") or 1) + 1
    notice = await callback.message.answer(t(lang, "plan.generating"))
    try:
        async with typing(callback.bot, callback.message.chat.id):
            result = await boxing_next_phase(services.plans, user, week=week, lang=lang)
    except (AIError, AIBudgetExceeded) as exc:
        logger.warning("boxing phase generation failed: %s", exc)
        await notice.edit_text(t(lang, "common.ai_busy"))
        return
    await _delete_quietly(notice)
    await send_long(callback.bot, callback.message.chat.id, result.text or t(lang, "common.error"),
                    reply_markup=plan_menu(lang, has_plan=True, kind="boxing"))


# ═══════════════════════════════════════════════════════════════════════════
#  today's session
# ═══════════════════════════════════════════════════════════════════════════
@router.callback_query(PlanCB.filter(F.action == "today"))
async def today_session(callback: CallbackQuery, user: User, lang: str, services: Services) -> None:
    await callback.answer()
    _session, text = await services.plans.session_for_today(user, lang=lang)
    if not text:
        await callback.message.answer(t(lang, "plan.none"), reply_markup=plan_menu(lang, has_plan=False))
        return
    plan = await services.repos.plans.any_active_plan(user.id)
    await send_long(callback.bot, callback.message.chat.id, text,
                    reply_markup=plan_menu(lang, has_plan=True, kind=plan.kind if plan else "training"))


@router.callback_query(PlanCB.filter(F.action == "mark_done"))
async def mark_done(callback: CallbackQuery, lang: str, state: FSMContext) -> None:
    await state.set_state(PlanFlow.instructions)
    await state.update_data(purpose="workout_feedback")
    await callback.answer()
    ar = lang.startswith("ar")
    await callback.message.answer(
        "💪 قبل ما أسجّلها: كم دقيقة تدرّبت وكم تقيّم الشدة من 10؟ (مثال: <code>45 دقيقة 8</code>)\n"
        "أو اكتب «تم» للتسجيل بدون تفاصيل."
        if ar
        else "💪 Before I log it: how many minutes and how hard (1-10)? (e.g. <code>45 min 8</code>)\n"
             "Or type “done” to log without details.",
        reply_markup=remove_keyboard(),
    )


@router.message(PlanFlow.instructions, F.text)
async def feedback(message: Message, user: User, lang: str, services: Services, state: FSMContext) -> None:
    data = await state.get_data()
    if data.get("purpose") != "workout_feedback":
        return
    await state.clear()
    text = (message.text or "").strip()
    numbers = [int(n) for n in text.replace(",", " ").replace("،", " ").split() if n.isdigit()]
    minutes = next((n for n in numbers if n > 10), None)
    rpe = next((n for n in numbers if 1 <= n <= 10 and n != minutes), None)

    session, info = await services.plans.complete_today(
        user, duration_min=minutes, rpe=rpe, feedback=truncate(text, 300), lang=lang
    )
    if session is None:
        # no programme session today → log the workout directly so the streak survives
        log = await services.repos.tracking.set_workout_done(
            user, note=truncate(text, 300) or "manual"
        )
        result = await services.streaks.register_activity(user, log)
        info = {"calories": log.burned_calories_est, "duration": minutes,
                "streak": result.streak, "badges": result.new_badges}
    reply = t(lang, "plan.workout_done", minutes=info.get("duration") or minutes or "—",
              calories=info.get("calories") or 0, streak=info.get("streak") or 0)
    for badge in info.get("badges") or []:
        reply += "\n" + t(lang, "streak.badge", badge=badge_label(badge, lang))
    plan = await services.repos.plans.any_active_plan(user.id)
    await send_long(message.bot, message.chat.id, reply,
                    reply_markup=plan_menu(lang, has_plan=plan is not None, kind=plan.kind if plan else None))


@router.callback_query(PlanCB.filter(F.action == "view"))
async def view_plan(callback: CallbackQuery, user: User, lang: str, services: Services) -> None:
    plan = await services.repos.plans.any_active_plan(user.id)
    if plan is None:
        await callback.answer(t(lang, "plan.none"), show_alert=True)
        return
    await answer_long(callback, services.plans.render_plan(plan, lang=lang))


@router.callback_query(PlanCB.filter(F.action == "menu"))
async def back(callback: CallbackQuery, user: User, lang: str, services: Services) -> None:
    await panel(callback, user, lang, services)


async def _delete_quietly(message: Message) -> None:
    try:
        await message.delete()
    except Exception:  # noqa: BLE001 — deleting the “generating…” notice is best-effort
        logger.debug("could not delete the progress notice")


__all__ = ["router", "panel", "VALID_KINDS"]
