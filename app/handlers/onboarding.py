"""Onboarding FSM (spec §2) — six mandatory steps + budget/food questions.

Every step validates its input and re-asks with a concrete example instead of
failing silently.  The final step computes BMR/TDEE/macros, runs the safety
screen over the injuries answer, grants the welcome bonus, attributes the
referral and seeds default reminders.
"""
from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.constants import BudgetLevel, Equipment, FoodStyle, Gender, Goal, InjuryArea
from app.db.models import User
from app.handlers.callbacks import OnboardingCB
from app.handlers.states import Onboarding
from app.i18n import t
from app.keyboards import (
    main_menu,
    onb_activity,
    onb_budget,
    onb_cancel,
    onb_equipment,
    onb_food_style,
    onb_gender,
    onb_goals,
    remove_keyboard,
)
from app.services.context import Services
from app.services.onboarding import summarize_for_user
from app.services.safety import detect_areas, static_screen
from app.utils import validators
from app.utils.text import truncate
from app.utils.tg import answer_long, notify

logger = logging.getLogger(__name__)

router = Router(name="onboarding")
GOAL_KEYS = {goal.value for goal in Goal}


async def _flush(services: Services, user: User, stage: str) -> None:
    user.onboarding_stage = stage
    await services.repos.session.flush()


# ═══════════════════════════════════════════════════════════════════════════
#  entry point
# ═══════════════════════════════════════════════════════════════════════════
async def begin_onboarding(
    event: Message | CallbackQuery, user: User, lang: str, services: Services, state: FSMContext
) -> None:
    await state.set_state(Onboarding.name)
    await state.update_data(injury_areas=[])
    await _flush(services, user, "name")
    await answer_long(event, t(lang, "onb.welcome"), reply_markup=onb_cancel(lang))


# ═══════════════════════════════════════════════════════════════════════════
#  step 1 — name
# ═══════════════════════════════════════════════════════════════════════════
@router.message(Onboarding.name, F.text)
async def step_name(message: Message, user: User, lang: str, state: FSMContext, services: Services) -> None:
    name = validators.safe_str(message.text, 60) or ""
    if len(name) < 2:
        await message.answer(t(lang, "common.error"))
        return
    user.display_name = name
    await _flush(services, user, "age")
    await state.set_state(Onboarding.age)
    await message.answer(t(lang, "onb.ask_age"), reply_markup=remove_keyboard())


# ═══════════════════════════════════════════════════════════════════════════
#  step 2 — age
# ═══════════════════════════════════════════════════════════════════════════
@router.message(Onboarding.age, F.text)
async def step_age(message: Message, user: User, lang: str, state: FSMContext, services: Services) -> None:
    age = validators.parse_age(message.text)
    if age is None:
        await message.answer(t(lang, "onb.invalid_number", example="27") + "\n" + t(lang, "onb.invalid_age"))
        return
    user.age = age
    await _flush(services, user, "gender")
    await state.set_state(Onboarding.gender)
    await message.answer(t(lang, "onb.ask_gender"), reply_markup=onb_gender(lang))


@router.callback_query(Onboarding.gender, OnboardingCB.filter(F.step == "gender"))
async def step_gender(
    callback: CallbackQuery, cb: OnboardingCB, user: User, lang: str, state: FSMContext, services: Services
) -> None:
    if cb.value not in {Gender.MALE.value, Gender.FEMALE.value}:
        await callback.answer()
        return
    user.gender = cb.value
    await _flush(services, user, "height")
    await state.set_state(Onboarding.height)
    await callback.answer()
    await callback.message.answer(t(lang, "onb.ask_height"), reply_markup=remove_keyboard())


# ═══════════════════════════════════════════════════════════════════════════
#  steps 3-5 — height, weight, target weight
# ═══════════════════════════════════════════════════════════════════════════
@router.message(Onboarding.height, F.text)
async def step_height(message: Message, user: User, lang: str, state: FSMContext, services: Services) -> None:
    height = validators.parse_height(message.text)
    if height is None:
        await message.answer(t(lang, "onb.invalid_number", example="175") + "\n" + t(lang, "onb.invalid_height"))
        return
    user.height_cm = height
    await _flush(services, user, "weight")
    await state.set_state(Onboarding.weight)
    await message.answer(t(lang, "onb.ask_weight"))


@router.message(Onboarding.weight, F.text)
async def step_weight(message: Message, user: User, lang: str, state: FSMContext, services: Services) -> None:
    weight = validators.parse_weight(message.text)
    if weight is None:
        await message.answer(t(lang, "onb.invalid_number", example="82.5") + "\n" + t(lang, "onb.invalid_weight"))
        return
    user.weight_kg = weight
    await services.repos.tracking.record_weight(user, weight, note="onboarding")
    await _flush(services, user, "target_weight")
    await state.set_state(Onboarding.target_weight)
    await message.answer(t(lang, "onb.ask_target_weight"))


@router.message(Onboarding.target_weight, F.text)
async def step_target_weight(
    message: Message, user: User, lang: str, state: FSMContext, services: Services
) -> None:
    text = (message.text or "").strip()
    if not validators.is_skip(text):
        weight = validators.parse_weight(text)
        if weight is None:
            await message.answer(t(lang, "onb.invalid_number", example="75"))
            return
        user.target_weight_kg = weight
    await _flush(services, user, "activity")
    await state.set_state(Onboarding.activity)
    await message.answer(t(lang, "onb.ask_activity"), reply_markup=onb_activity(lang))


@router.callback_query(Onboarding.activity, OnboardingCB.filter(F.step == "activity"))
async def step_activity(
    callback: CallbackQuery, cb: OnboardingCB, user: User, lang: str, state: FSMContext, services: Services
) -> None:
    user.activity_level = cb.value
    await _flush(services, user, "goal")
    await state.set_state(Onboarding.goal)
    await callback.answer()
    await callback.message.answer(t(lang, "onb.ask_goal"), reply_markup=onb_goals(lang))


# ═══════════════════════════════════════════════════════════════════════════
#  step 6 — goal (with a free-text custom option)
# ═══════════════════════════════════════════════════════════════════════════
@router.callback_query(Onboarding.goal, OnboardingCB.filter(F.step == "goal"))
async def step_goal(
    callback: CallbackQuery, cb: OnboardingCB, user: User, lang: str, state: FSMContext, services: Services
) -> None:
    if cb.value == Goal.CUSTOM.value:
        await state.set_state(Onboarding.goal_custom)
        await callback.answer()
        await callback.message.answer(t(lang, "onb.ask_goal_custom"), reply_markup=remove_keyboard())
        return
    if cb.value not in GOAL_KEYS:
        await callback.answer()
        return
    user.goal = cb.value
    await _flush(services, user, "equipment")
    await state.set_state(Onboarding.equipment)
    await callback.answer()
    await callback.message.answer(t(lang, "onb.ask_equipment"), reply_markup=onb_equipment(lang))


@router.message(Onboarding.goal_custom, F.text)
async def step_goal_custom(
    message: Message, user: User, lang: str, state: FSMContext, services: Services
) -> None:
    note = truncate((message.text or "").strip(), 500)
    if len(note) < 5:
        await message.answer(t(lang, "onb.ask_goal_custom"))
        return
    user.goal = Goal.CUSTOM.value
    user.goal_custom_note = note
    await _flush(services, user, "equipment")
    await state.set_state(Onboarding.equipment)
    await message.answer(t(lang, "onb.ask_equipment"), reply_markup=onb_equipment(lang))


@router.callback_query(Onboarding.equipment, OnboardingCB.filter(F.step == "equipment"))
async def step_equipment(
    callback: CallbackQuery, cb: OnboardingCB, user: User, lang: str, state: FSMContext, services: Services
) -> None:
    if cb.value not in {item.value for item in Equipment}:
        await callback.answer()
        return
    user.equipment = cb.value
    await _flush(services, user, "injuries")
    await state.set_state(Onboarding.injuries)
    await callback.answer()
    await callback.message.answer(
        t(lang, "onb.ask_injuries") + "\n\n" + _injury_hint(lang), reply_markup=_injury_keyboard(lang)
    )


def _injury_hint(lang: str) -> str:
    return (
        "اختار من الأزرار ثم اكتب «تم»، أو اكتب تفاصيل إصابتك/حالتك الصحية برسالة."
        if lang.startswith("ar")
        else "Tap the buttons then type “done”, or write your injury/condition in a message."
    )


def _injury_keyboard(lang: str) -> InlineKeyboardBuilder:
    ar = lang.startswith("ar")
    labels = {
        InjuryArea.NONE.value: ("✅ لا شيء" if ar else "✅ None"),
        InjuryArea.KNEE.value: ("🦵 ركبة" if ar else "🦵 Knee"),
        InjuryArea.LOWER_BACK.value: ("🧍 أسفل الظهر" if ar else "🧍 Lower back"),
        InjuryArea.SHOULDER.value: ("💪 كتف" if ar else "💪 Shoulder"),
        InjuryArea.NECK.value: ("🦒 رقبة" if ar else "🦒 Neck"),
        InjuryArea.ANKLE.value: ("🦶 كاحل" if ar else "🦶 Ankle"),
        InjuryArea.WRIST.value: ("✋ رسغ" if ar else "✋ Wrist"),
        InjuryArea.CHEST_HEART.value: ("❤️ قلب/صدر" if ar else "❤️ Heart/chest"),
        InjuryArea.OTHER.value: ("➕ أخرى" if ar else "➕ Other"),
    }
    builder = InlineKeyboardBuilder()
    row: list[InlineKeyboardButton] = []
    for key, label in labels.items():
        row.append(InlineKeyboardButton(text=label, callback_data=OnboardingCB(step="injury", value=key).pack()))
        if len(row) == 2:
            builder.row(*row)
            row = []
    if row:
        builder.row(*row)
    return builder


@router.callback_query(Onboarding.injuries, OnboardingCB.filter(F.step == "injury"))
async def step_injury_choice(callback: CallbackQuery, cb: OnboardingCB, lang: str, state: FSMContext) -> None:
    """Quick-pick injury areas for users who prefer tapping over typing."""
    data = await state.get_data()
    areas: list[str] = list(data.get("injury_areas") or [])
    if cb.value == InjuryArea.NONE.value:
        areas = []
    elif cb.value not in areas:
        areas.append(cb.value)
    await state.update_data(injury_areas=areas)
    ar = lang.startswith("ar")
    label = "، ".join(areas) if areas else ("لا شيء" if ar else "none")
    await callback.answer(f"✅ {label}")


# ═══════════════════════════════════════════════════════════════════════════
#  step 7 — injuries / conditions (safety-critical)
# ═══════════════════════════════════════════════════════════════════════════
@router.message(Onboarding.injuries, F.text)
async def step_injuries(
    message: Message, user: User, lang: str, state: FSMContext, services: Services
) -> None:
    data = await state.get_data()
    picked: list[str] = list(data.get("injury_areas") or [])
    text = (message.text or "").strip()
    negative = validators.is_negative(text) or validators.is_skip(text)

    if negative and not picked:
        user.injuries = []
        user.health_conditions = []
        user.requires_medical_clearance = False
    else:
        combined = ", ".join([*picked, *(detect_areas(text) if not negative else [])])
        areas = list(dict.fromkeys(a for a in combined.split(", ") if a and a != InjuryArea.NONE.value))
        screen = static_screen(", ".join(areas) if areas else text, lang)
        user.injuries = [
            {"area": area, "note": truncate(text, 160), "severity": "reported"} for area in areas
        ] or ([{"area": InjuryArea.OTHER.value, "note": truncate(text, 300), "severity": "reported"}]
              if text and not negative else [])
        user.health_conditions = screen.conditions
        user.requires_medical_clearance = screen.red_flag
        user.medical_notes = truncate(text, 500) if text and not negative else None
        user.preferences = {
            **(user.preferences or {}),
            "restricted_movements": screen.restricted,
            "safe_alternatives": screen.alternatives,
            "medical_referral": screen.referral,
        }
    await _flush(services, user, "budget")
    await state.set_state(Onboarding.budget)
    await message.answer(t(lang, "onb.ask_budget"), reply_markup=onb_budget(lang))


# ═══════════════════════════════════════════════════════════════════════════
#  steps 8-10 — budget, food style, disliked foods
# ═══════════════════════════════════════════════════════════════════════════
@router.callback_query(Onboarding.budget, OnboardingCB.filter(F.step == "budget"))
async def step_budget(
    callback: CallbackQuery, cb: OnboardingCB, user: User, lang: str, state: FSMContext, services: Services
) -> None:
    if cb.value not in {item.value for item in BudgetLevel}:
        await callback.answer()
        return
    user.budget_level = cb.value
    await _flush(services, user, "food_style")
    await state.set_state(Onboarding.food_style)
    await callback.answer()
    await callback.message.answer(t(lang, "onb.ask_food_style"), reply_markup=onb_food_style(lang))


@router.callback_query(Onboarding.food_style, OnboardingCB.filter(F.step == "food"))
async def step_food_choice(
    callback: CallbackQuery, cb: OnboardingCB, user: User, lang: str, state: FSMContext, services: Services
) -> None:
    ar = lang.startswith("ar")
    if cb.value == "_custom":
        await state.set_state(Onboarding.food_custom)
        await callback.answer()
        await callback.message.answer(
            "اكتب نوع أكلك بالتفصيل (مثال: أكل شعبي سوري، رز وبرغل وفول، وما بحب السمك)."
            if ar
            else "Describe your usual food (e.g. local home cooking, rice and beans, no fish).",
            reply_markup=remove_keyboard(),
        )
        return
    if cb.value not in {item.value for item in FoodStyle}:
        await callback.answer()
        return
    user.food_style = cb.value
    await _flush(services, user, "disliked")
    await state.set_state(Onboarding.disliked)
    await callback.answer()
    await callback.message.answer(t(lang, "onb.ask_disliked"), reply_markup=remove_keyboard())


@router.message(Onboarding.food_custom, F.text)
async def step_food_custom(
    message: Message, user: User, lang: str, state: FSMContext, services: Services
) -> None:
    note = truncate((message.text or "").strip(), 400)
    user.food_style = FoodStyle.LOCAL_POPULAR.value
    user.food_style_note = note
    await _flush(services, user, "disliked")
    await state.set_state(Onboarding.disliked)
    await message.answer(t(lang, "onb.ask_disliked"), reply_markup=remove_keyboard())


@router.message(Onboarding.disliked, F.text)
async def step_disliked(
    message: Message, user: User, lang: str, state: FSMContext, services: Services
) -> None:
    text = (message.text or "").strip()
    if not (validators.is_negative(text) or validators.is_skip(text)):
        items = [item.strip() for item in text.replace("،", ",").split(",") if item.strip()][:12]
        user.disliked_foods = items
        user.allergies = [item for item in items if _looks_like_allergy(item)]
    await services.repos.session.flush()
    await finish(message, user, lang, state, services)


# ═══════════════════════════════════════════════════════════════════════════
#  completion
# ═══════════════════════════════════════════════════════════════════════════
async def finish(
    event: Message | CallbackQuery, user: User, lang: str, state: FSMContext, services: Services
) -> None:
    data = await state.get_data()
    areas: list[str] = list(data.get("injury_areas") or [])
    injury_text = ", ".join(areas) or (user.medical_notes or None)
    if user.injuries and not injury_text:
        injury_text = ", ".join(str(item.get("area")) for item in user.injuries if item.get("area"))

    result = await services.onboarding.finalize(user, injury_text=injury_text, lang=lang)
    await state.clear()

    from app.handlers.start import is_admin

    await answer_long(
        event,
        summarize_for_user(result, lang),
        reply_markup=main_menu(lang, is_admin=is_admin(event.from_user.id), has_coach=user.has_coach_access),
    )

    # tell the inviter their referral qualified (spec extra §6)
    if result.referral is not None and services.bot is not None and result.referral.inviter:
        note = await services.referral.notify_inviter_payload(result.referral, lang)
        if note:
            await notify(services.bot, result.referral.inviter.tg_id, note)


def _looks_like_allergy(item: str) -> bool:
    words = ("حساس", "الرجي", "aller", "لا أستطيع أكل", "ما اقدر", "ما بقدر")
    return any(word in item.lower() for word in words)


# ═══════════════════════════════════════════════════════════════════════════
#  cancel / stray input
# ═══════════════════════════════════════════════════════════════════════════
@router.callback_query(OnboardingCB.filter(F.step == "cancel"))
async def cancel_onboarding(callback: CallbackQuery, lang: str, state: FSMContext) -> None:
    await state.clear()
    await callback.answer(t(lang, "common.cancel"))
    ar = lang.startswith("ar")
    await callback.message.answer(
        "أُلغي التسجيل. أرسل /start متى ما حبيت تكمل." if ar else "Sign-up cancelled. Send /start to continue.",
        reply_markup=remove_keyboard(),
    )


@router.message(Onboarding(), ~F.text)
async def onboarding_non_text(message: Message, lang: str) -> None:
    ar = lang.startswith("ar")
    await message.answer(
        "أكمل التسجيل من فضلك — الردود نصية أو من الأزرار." if ar else "Please continue the sign-up — text or buttons."
    )


__all__ = ["router", "begin_onboarding", "finish", "GOAL_KEYS"]
