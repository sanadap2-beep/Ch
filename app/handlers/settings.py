"""Settings: language, profile edits, goal, timezone, reminders, privacy (export/delete)."""
from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.config import settings as app_settings
from app.constants import ActivityLevel, BudgetLevel, Equipment, FoodStyle, Goal
from app.db.models import User
from app.handlers.callbacks import MenuCB, PrivacyCB, ReminderCB, SettingsCB
from app.handlers.states import ProfileEdit, ReminderFlow
from app.i18n import lock_language, t
from app.keyboards import (
    ACTIVITY_LABELS,
    BUDGET_LABELS,
    EQUIPMENT_LABELS,
    FOOD_LABELS,
    GENDER_LABELS,
    _label,
    confirm_deletion,
    language_menu,
    privacy_menu,
    profile_fields,
    reminders_menu,
    remove_keyboard,
    settings_menu,
)
from app.services.context import Services
from app.services.goal_registry import all_goals, get_goal
from app.services.metabolism import NutritionTargets
from app.services.reminders import kind_label
from app.services.safety import detect_areas, static_screen
from app.utils import validators
from app.utils.text import escape_html, truncate
from app.utils.tg import edit_or_send, send_document_bytes, send_long

logger = logging.getLogger(__name__)

router = Router(name="settings")

MENU_LABELS = {"⚙️ الإعدادات", "⚙️ Settings"}
FIELD_KEYS = {"age", "weight", "height", "activity", "equipment", "budget", "food", "injuries"}


# ═══════════════════════════════════════════════════════════════════════════
#  panel
# ═══════════════════════════════════════════════════════════════════════════
def _markup(user: User, lang: str):
    return settings_menu(lang, tz=user.user_timezone or "UTC")


async def panel(event: Message | CallbackQuery, user: User, lang: str) -> None:
    text = profile_summary(user, lang)
    if isinstance(event, CallbackQuery):
        await edit_or_send(event, text, reply_markup=_markup(user, lang))
    else:
        await send_long(event.bot, event.chat.id, text, reply_markup=_markup(user, lang))


@router.message(F.text.in_(MENU_LABELS))
async def menu_button(message: Message, user: User, lang: str) -> None:
    await panel(message, user, lang)


@router.callback_query(MenuCB.filter(F.action == "settings"))
async def menu_callback(callback: CallbackQuery, user: User, lang: str) -> None:
    await panel(callback, user, lang)


@router.callback_query(SettingsCB.filter(F.action == "menu"))
async def settings_menu_cb(callback: CallbackQuery, user: User, lang: str) -> None:
    await panel(callback, user, lang)


def profile_summary(user: User, lang: str) -> str:
    ar = lang.startswith("ar")
    gender = _label(GENDER_LABELS, user.gender or "", lang) if user.gender else "—"
    lines = [
        t(lang, "set.title"),
        f"👤 {escape_html(user.full_name)} · {user.age or '—'} "
        f"{'سنة' if ar else 'y'} · {gender}",
        f"📏 {user.height_cm or '—'} {'سم' if ar else 'cm'} · ⚖️ {user.weight_kg or '—'} "
        f"{'كجم' if ar else 'kg'}"
        + (f" · 🎯 {user.target_weight_kg} {'كجم' if ar else 'kg'}" if user.target_weight_kg else ""),
        f"🔥 {int(user.target_calories or 0)} "
        f"{'سعرة' if ar else 'kcal'} · "
        f"{'بروتين' if ar else 'P'} {int(user.target_protein_g or 0)} / "
        f"{'كارب' if ar else 'C'} {int(user.target_carbs_g or 0)} / "
        f"{'دهون' if ar else 'F'} {int(user.target_fats_g or 0)} "
        f"{'جم' if ar else 'g'}",
        f"🎯 {get_goal(user.goal).describe(lang)}"
        + (f" — {escape_html(truncate(user.goal_custom_note, 120))}" if user.goal_custom_note else ""),
        f"🚶 {_label(ACTIVITY_LABELS, user.activity_level or '', lang) if user.activity_level else '—'} · "
        f"🧰 {_label(EQUIPMENT_LABELS, user.equipment or '', lang) if user.equipment else '—'}",
        f"💰 {_label(BUDGET_LABELS, user.budget_level or '', lang) if user.budget_level else '—'} · "
        f"🍲 {_label(FOOD_LABELS, user.food_style or '', lang) if user.food_style else '—'}",
    ]
    if user.injuries:
        areas = "، ".join(sorted({str(item.get("area")) for item in user.injuries if item.get("area")}))
        lines.append(f"🚨 {escape_html(areas)}")
    if user.allergies:
        lines.append(f"⛔ {'حساسية' if ar else 'Allergies'}: "
                     f"{', '.join(escape_html(str(a)) for a in user.allergies)}")
    if user.disliked_foods:
        lines.append(f"🙅 {'ما أحب' if ar else 'Dislikes'}: "
                     f"{', '.join(escape_html(str(d)) for d in list(user.disliked_foods)[:8])}")
    lines.append(f"🌍 {user.language_code or 'ar'} · 🕒 {user.user_timezone or 'UTC'}")
    return "\n".join(lines)


def _targets_text(targets: NutritionTargets, lang: str) -> str:
    ar = lang.startswith("ar")
    lines = [
        f"<b>{'🧮 خطتك الجديدة' if ar else '🧮 Your new plan'}</b>",
        f"BMR: {int(targets.bmr)} · TDEE: {int(targets.tdee)}",
        f"🔥 {targets.calories} {'سعرة/يوم' if ar else 'kcal/day'} — "
        f"{'بروتين' if ar else 'P'} {targets.protein_g} / "
        f"{'كارب' if ar else 'C'} {targets.carbs_g} / "
        f"{'دهون' if ar else 'F'} {targets.fats_g} {'جم' if ar else 'g'}",
        f"💧 {targets.water_ml} {'مل ماء' if ar else 'ml water'}",
    ]
    if targets.bmi:
        lines.append(f"BMI: {targets.bmi:.1f}")
    for note in (targets.notes or [])[:3]:
        lines.append(f"ℹ️ {truncate(str(note), 160)}")
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════════════
#  language
# ═══════════════════════════════════════════════════════════════════════════
@router.callback_query(SettingsCB.filter(F.action == "language"))
async def language_panel(callback: CallbackQuery, lang: str) -> None:
    await edit_or_send(callback, t(lang, "set.language"), reply_markup=language_menu(lang))


@router.callback_query(SettingsCB.filter(F.action == "lang"))
async def language_set(callback: CallbackQuery, callback_data: SettingsCB, user: User, services: Services) -> None:
    user.language_code = callback_data.arg if callback_data.arg in {"ar", "en"} else "ar"
    lock_language(user)
    await services.repos.session.flush()
    lang = user.language_code
    await callback.answer(t(lang, "lang.changed"))
    await panel(callback, user, lang)


# ═══════════════════════════════════════════════════════════════════════════
#  profile editing
# ═══════════════════════════════════════════════════════════════════════════
@router.callback_query(SettingsCB.filter(F.action == "profile"))
async def profile_panel(callback: CallbackQuery, lang: str) -> None:
    await edit_or_send(callback, t(lang, "set.profile"), reply_markup=profile_fields(lang))


FIELD_PROMPTS = {
    "weight": ("اكتب وزنك الحالي بالكجم (مثال: <code>81.4</code>):", "Your current weight in kg (e.g. <code>81.4</code>):"),
    "height": ("اكتب طولك بالسم (مثال: <code>175</code>):", "Your height in cm (e.g. <code>175</code>):"),
    "age": ("اكتب عمرك (مثال: <code>27</code>):", "Your age (e.g. <code>27</code>):"),
    "activity": ("اكتب مستوى نشاطك: sedentary / light / moderate / very_active / athlete",
                 "Your activity level: sedentary / light / moderate / very_active / athlete"),
    "equipment": ("اكتب معداتك: gym / home_basic / home_no_equipment / calisthenics_park",
                  "Your equipment: gym / home_basic / home_no_equipment / calisthenics_park"),
    "budget": ("اكتب ميزانيتك: low / medium / high", "Your budget: low / medium / high"),
    "food": ("اكتب نوع أكلك بالتفصيل (مثال: أكل شعبي، رز وبرغل، وما بحب السمك):",
             "Describe your usual food (e.g. local home cooking, no fish):"),
    "injuries": ("اكتب إصاباتك/حالتك الصحية، أو «لا شيء»:", "Your injuries/conditions, or “none”:"),
}


@router.callback_query(SettingsCB.filter(F.action == "field"))
async def field_prompt(callback: CallbackQuery, callback_data: SettingsCB, lang: str, state: FSMContext) -> None:
    field = callback_data.arg
    if field not in FIELD_KEYS:
        await callback.answer()
        return
    await state.set_state(ProfileEdit.field)
    await state.update_data(field=field)
    ar_prompt, en_prompt = FIELD_PROMPTS[field]
    await callback.answer()
    await callback.message.answer(ar_prompt if lang.startswith("ar") else en_prompt,
                                  reply_markup=remove_keyboard())


@router.message(ProfileEdit.field, F.text)
async def field_value(
    message: Message, user: User, lang: str, services: Services, state: FSMContext
) -> None:
    data = await state.get_data()
    field = data.get("field")
    if not field:
        await state.clear()
        return
    raw = (message.text or "").strip()
    recompute = False

    if field == "weight":
        weight = validators.parse_weight(raw)
        if weight is None:
            await message.answer(t(lang, "onb.invalid_number", example="81.4"))
            return
        user.weight_kg = weight
        await services.repos.tracking.record_weight(user, weight, note="settings")
        recompute = True
    elif field == "height":
        height = validators.parse_height(raw)
        if height is None:
            await message.answer(t(lang, "onb.invalid_number", example="175"))
            return
        user.height_cm = height
        recompute = True
    elif field == "age":
        age = validators.parse_age(raw)
        if age is None:
            await message.answer(t(lang, "onb.invalid_number", example="27"))
            return
        user.age = age
        recompute = True
    elif field == "activity":
        key = raw.lower().replace(" ", "_")
        if key not in {level.value for level in ActivityLevel}:
            await message.answer(FIELD_PROMPTS["activity"][0 if lang.startswith("ar") else 1])
            return
        user.activity_level = key
        recompute = True
    elif field == "equipment":
        key = raw.lower().replace(" ", "_")
        if key not in {item.value for item in Equipment}:
            await message.answer(FIELD_PROMPTS["equipment"][0 if lang.startswith("ar") else 1])
            return
        user.equipment = key
    elif field == "budget":
        key = raw.lower()
        if key not in {item.value for item in BudgetLevel}:
            await message.answer(FIELD_PROMPTS["budget"][0 if lang.startswith("ar") else 1])
            return
        user.budget_level = key
    elif field == "food":
        key = raw.lower()
        user.food_style = key if key in {item.value for item in FoodStyle} else FoodStyle.LOCAL_POPULAR.value
        user.food_style_note = truncate(raw, 300)
    elif field == "injuries":
        if validators.is_negative(raw) or validators.is_skip(raw):
            user.injuries = []
            user.health_conditions = []
            user.requires_medical_clearance = False
            user.medical_notes = None
        else:
            areas = detect_areas(raw) or ["other"]
            screen = static_screen(raw, lang)
            user.injuries = [{"area": area, "note": truncate(raw, 160), "severity": "reported"} for area in areas]
            user.health_conditions = screen.conditions
            user.requires_medical_clearance = screen.red_flag
            user.medical_notes = truncate(raw, 500)
            user.preferences = {
                **(user.preferences or {}),
                "restricted_movements": screen.restricted,
                "safe_alternatives": screen.alternatives,
                "medical_referral": screen.referral,
            }
            if screen.red_flag:
                await message.answer(t(lang, "onb.safety_warning", detail=escape_html(screen.notes or raw[:200]),
                                       referral=escape_html(screen.referral or "—"),
                                       restricted=", ".join(screen.restricted) or "—",
                                       alternatives=", ".join(screen.alternatives) or "—"))

    await services.repos.session.flush()
    text = t(lang, "set.saved")
    if recompute and user.onboarding_done:
        targets = await services.onboarding.recompute(user, reason=f"profile update: {field}")
        text += "\n\n" + _targets_text(targets, lang)
    await state.clear()
    await send_long(message.bot, message.chat.id, text, reply_markup=_markup(user, lang))


# ═══════════════════════════════════════════════════════════════════════════
#  goal & timezone
# ═══════════════════════════════════════════════════════════════════════════
@router.callback_query(SettingsCB.filter(F.action == "goal"))
async def goal_prompt(callback: CallbackQuery, lang: str, state: FSMContext) -> None:
    await state.set_state(ProfileEdit.goal_custom)
    ar = lang.startswith("ar")
    options = "\n".join(f"• <code>{goal['key']}</code> — {escape_html(goal['label'])}"
                        for goal in all_goals(lang) if goal["key"] != Goal.CUSTOM.value)
    await callback.answer()
    await callback.message.answer(
        (f"{t(lang, 'set.goal')}\n\n{options}\n\nاكتب اسم الهدف (أو «custom» لهدف مخصص):")
        if ar
        else (f"{t(lang, 'set.goal')}\n\n{options}\n\nType the goal key (or “custom”):"),
        reply_markup=remove_keyboard(),
    )


@router.message(ProfileEdit.goal_custom, F.text)
async def goal_value(
    message: Message, user: User, lang: str, services: Services, state: FSMContext
) -> None:
    raw = (message.text or "").strip()
    key = raw.lower()
    if key in {"custom", "مخصص", "مخصص goal"}:
        await state.set_state(ProfileEdit.injuries)  # reuse: collect the custom goal note
        await state.update_data(field="goal_note")
        ar = lang.startswith("ar")
        await message.answer("اكتب هدفك بكلماتك (مثال: أرجع أتسلق جبال بعد 3 أشهر):"
                             if ar else "Describe your goal in your own words:")
        return
    goal = next((g for g in all_goals(lang) if g["key"] == key), None)
    if goal is None:
        await message.answer(t(lang, "common.error"))
        return
    targets = await services.onboarding.change_goal(user, goal["key"])
    await services.repos.session.flush()
    await state.clear()
    await send_long(message.bot, message.chat.id,
                    t(lang, "set.saved") + "\n\n" + _targets_text(targets, lang),
                    reply_markup=_markup(user, lang))


@router.message(ProfileEdit.injuries, F.text)
async def goal_note(
    message: Message, user: User, lang: str, services: Services, state: FSMContext
) -> None:
    data = await state.get_data()
    if data.get("field") != "goal_note":
        return
    note = truncate((message.text or "").strip(), 300)
    if len(note) < 5:
        await message.answer(t(lang, "common.error"))
        return
    targets = await services.onboarding.change_goal(user, Goal.CUSTOM.value, custom_note=note)
    await services.repos.session.flush()
    await state.clear()
    await send_long(message.bot, message.chat.id,
                    t(lang, "set.saved") + "\n\n" + _targets_text(targets, lang),
                    reply_markup=_markup(user, lang))


@router.callback_query(SettingsCB.filter(F.action == "timezone"))
async def timezone_prompt(callback: CallbackQuery, user: User, lang: str, state: FSMContext) -> None:
    await state.set_state(ProfileEdit.timezone)
    ar = lang.startswith("ar")
    await callback.answer()
    await callback.message.answer(
        f"{t(lang, 'set.timezone', tz=user.user_timezone or 'UTC')}\n\n"
        + ("اكتب منطقتك الزمنية (مثال: <code>Asia/Damascus</code>):" if ar
           else "Type your timezone (e.g. <code>Asia/Damascus</code>):"),
        reply_markup=remove_keyboard(),
    )


@router.message(ProfileEdit.timezone, F.text)
async def timezone_value(
    message: Message, user: User, lang: str, services: Services, state: FSMContext
) -> None:
    zone = validators.parse_timezone(message.text)
    if zone is None:
        ar = lang.startswith("ar")
        await message.answer("منطقة زمنية غير معروفة 🤔 مثال: <code>Asia/Damascus</code>"
                             if ar else "Unknown timezone 🤔 e.g. <code>Asia/Damascus</code>")
        return
    user.user_timezone = zone
    await services.repos.session.flush()
    await services.reminders.reschedule_all(user)
    await state.clear()
    await send_long(message.bot, message.chat.id, t(lang, "set.saved"), reply_markup=_markup(user, lang))


# ═══════════════════════════════════════════════════════════════════════════
#  reminders (spec extra §1)
# ═══════════════════════════════════════════════════════════════════════════
@router.callback_query(SettingsCB.filter(F.action == "reminders"))
async def reminders_panel(callback: CallbackQuery, user: User, lang: str, services: Services) -> None:
    await services.reminders.ensure_defaults(user)
    rules = await services.reminders.rules(user)
    text = t(lang, "rem.title", list=services.reminders.render_list(rules, user, lang))
    await edit_or_send(callback, text, reply_markup=reminders_menu(lang, rules))


@router.callback_query(ReminderCB.filter(F.action == "toggle"))
async def toggle_reminder(
    callback: CallbackQuery, callback_data: ReminderCB, user: User, lang: str, services: Services
) -> None:
    rule = await services.reminders.toggle(user, callback_data.kind)
    label = kind_label(rule.kind, lang)
    when = (
        rule.next_run_at.strftime("%H:%M")
        if rule.next_run_at
        else (f"{rule.hour:02d}:{rule.minute:02d}" if rule.hour is not None else "—")
    )
    await callback.answer(t(lang, "rem.on", kind=label, time=when) if rule.enabled
                          else t(lang, "rem.off", kind=label))
    await reminders_panel(callback, user, lang, services)


@router.callback_query(ReminderCB.filter(F.action == "interval"))
async def reminder_interval(
    callback: CallbackQuery, callback_data: ReminderCB, user: User, lang: str, services: Services
) -> None:
    minutes = validators.parse_int(callback_data.arg, min_value=30, max_value=720) or app_settings.water_reminder_interval_min
    rule = await services.reminders.upsert(user, callback_data.kind, enabled=True, interval_minutes=minutes)
    rule.next_run_at = None
    await services.reminders.reschedule_all(user)
    await callback.answer(t(lang, "set.saved"))
    await reminders_panel(callback, user, lang, services)


@router.callback_query(ReminderCB.filter(F.action == "time"))
async def reminder_time_prompt(callback: CallbackQuery, callback_data: ReminderCB, lang: str, state: FSMContext) -> None:
    await state.set_state(ReminderFlow.time)
    await state.update_data(kind=callback_data.kind)
    await callback.answer()
    ar = lang.startswith("ar")
    await callback.message.answer("اكتب الوقت الجديد (مثال: <code>14:30</code>):" if ar
                                  else "Type the new time (e.g. <code>14:30</code>):",
                                  reply_markup=remove_keyboard())


@router.message(ReminderFlow.time, F.text)
async def reminder_time_value(
    message: Message, user: User, lang: str, services: Services, state: FSMContext
) -> None:
    data = await state.get_data()
    kind = data.get("kind")
    if not kind:
        await state.clear()
        return
    value = validators.parse_time_of_day(message.text)
    if value is None:
        await message.answer("صيغة الوقت: <code>14:30</code>")
        return
    hour, minute = value
    rule = await services.reminders.upsert(user, kind, enabled=True, hour=hour, minute=minute)
    await services.reminders.reschedule_all(user)
    await state.clear()
    await send_long(
        message.bot, message.chat.id,
        t(lang, "rem.on", kind=kind_label(kind, lang), time=rule.next_run_at.strftime("%H:%M")
          if rule.next_run_at else f"{hour:02d}:{minute:02d}"),
        reply_markup=_markup(user, lang),
    )


# ═══════════════════════════════════════════════════════════════════════════
#  privacy (spec extra §14) — policy / export / delete
# ═══════════════════════════════════════════════════════════════════════════
@router.callback_query(SettingsCB.filter(F.action == "privacy"))
@router.callback_query(PrivacyCB.filter(F.action == "menu"))
async def privacy_panel(callback: CallbackQuery, lang: str, services: Services) -> None:
    text = f"<b>{t(lang, 'privacy.title')}</b>\n\n{services.privacy.policy_text(lang)}"
    await edit_or_send(callback, text, reply_markup=privacy_menu(lang))


@router.callback_query(PrivacyCB.filter(F.action == "policy"))
async def privacy_policy(callback: CallbackQuery, lang: str, services: Services) -> None:
    await privacy_panel(callback, lang, services)


@router.callback_query(PrivacyCB.filter(F.action == "export"))
async def export_data(callback: CallbackQuery, user: User, lang: str, services: Services) -> None:
    await callback.answer()
    ar = lang.startswith("ar")
    status = await callback.message.answer("⏳ " + ("أجهّز بياناتك…" if ar else "Preparing your data…"))
    cost = app_settings.data_export_cost_points
    if cost:
        from app.constants import PointsReason
        from app.db.repositories.economy import InsufficientPoints

        try:
            await services.points.charge_flat(user, cost, PointsReason.REPORT_EXPORT, note="data export")
        except InsufficientPoints as exc:
            await status.edit_text(t(lang, "coach.insufficient", need=exc.required, have=exc.available))
            return
    try:
        pdf = await services.privacy.export_pdf(user, lang=lang)
    except Exception:  # noqa: BLE001 — never crash on export
        logger.exception("privacy export failed")
        await status.edit_text(t(lang, "common.error"))
        return
    await status.delete()
    await send_document_bytes(
        callback.bot, callback.message.chat.id, pdf, filename=f"my-data-{user.tg_id}.pdf",
        caption=t(lang, "set.export"),
    )


@router.callback_query(PrivacyCB.filter(F.action == "delete_photos"))
async def delete_photos(callback: CallbackQuery, user: User, lang: str, services: Services) -> None:
    count = await services.privacy.delete_photos(user, admin=callback.from_user.id)
    await callback.answer(t(lang, "set.delete_photos"))
    await edit_or_send(callback, t(lang, "privacy.deleted", count=count), reply_markup=privacy_menu(lang))


@router.callback_query(PrivacyCB.filter(F.action == "delete_account"))
async def ask_delete(callback: CallbackQuery, lang: str) -> None:
    await edit_or_send(callback, t(lang, "set.delete_all"), reply_markup=confirm_deletion(lang))


@router.callback_query(PrivacyCB.filter(F.action == "confirm_delete"))
async def confirm_delete(callback: CallbackQuery, user: User, lang: str, services: Services) -> None:
    result = await services.privacy.delete_account(user, admin=callback.from_user.id)
    total = sum(int(value) for value in result.values() if isinstance(value, int))
    await callback.answer(t(lang, "privacy.deleted", count=total), show_alert=True)
    ar = lang.startswith("ar")
    await callback.message.answer(
        t(lang, "privacy.deleted", count=total) + "\n" + ("وداعًا 👋" if ar else "Goodbye 👋"),
        reply_markup=remove_keyboard(),
    )
