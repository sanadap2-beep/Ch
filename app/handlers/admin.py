"""Admin panel (spec §7): stats, users, manual points, broadcasts, routing, audit."""
from __future__ import annotations

import csv
import io
import logging
import uuid
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.ai import router as ai_router
from app.config import settings
from app.constants import AdminAction as AdminActionEnum
from app.constants import AITask, BroadcastStatus, SubscriptionPlan
from app.db.models import User
from app.db.repositories.economy import InsufficientPoints
from app.handlers.callbacks import AdminCB, AdminUserCB
from app.handlers.states import AdminFlow
from app.i18n import t
from app.keyboards import (
    admin_broadcast,
    admin_channels,
    admin_config,
    admin_goal_filter,
    admin_menu,
    admin_user_card,
    admin_users_pager,
    remove_keyboard,
    subscription_label,
)
from app.services.context import Services
from app.services.goal_registry import get_goal
from app.utils.text import compact_number, escape_html, mask_id
from app.utils.tg import edit_or_send, notify, send_document_bytes, send_long
from app.utils.validators import normalize_digits, parse_float, parse_int, safe_str

logger = logging.getLogger(__name__)

router = Router(name="admin")
PER_PAGE = 10
AUDIENCES = ("all", "active", "coaches", "onboarded")
ADMIN_LABELS = {"🛡️ لوحة الأدمن", "🛡️ Admin panel"}


# ═══════════════════════════════════════════════════════════════════════════
#  entry points
# ═══════════════════════════════════════════════════════════════════════════
async def _menu_text(services: Services, lang: str) -> str:
    ar = lang.startswith("ar")
    stats = await services.repos.users.activity_stats()
    usage = await services.usage.platform(days=7)
    return "\n".join([
        t(lang, "adm.title"),
        f"👥 {stats['total']} {'مستخدم' if ar else 'users'} · "
        f"{'اليوم' if ar else 'today'} {stats['today']} · "
        f"{'الأسبوع' if ar else 'week'} {stats['week']} · "
        f"{'30 يوم' if ar else '30d'} {stats['month']}",
        f"✅ {stats['onboarded']} {'مكتمل' if ar else 'onboarded'} · "
        f"🏆 {stats['coaches']} {'كوتش' if ar else 'coach'} · "
        f"⛔ {stats['banned']} {'محظور' if ar else 'banned'}",
        f"💵 {'تكلفة اليوم' if ar else 'Today'}: ${usage['today_cost_usd']:.3f} / "
        f"${settings.global_daily_usd_cap:.2f} ({usage['cap_used_pct']}%) · "
        f"{'7 أيام' if ar else '7d'}: ${usage['total_cost_usd']:.2f}",
    ])


@router.message(Command("admin"))
@router.message(F.text.in_(ADMIN_LABELS))
async def admin_entry(
    message: Message, lang: str, services: Services, is_admin: bool = False
) -> None:
    if not is_admin:
        await message.answer("⛔ " + t(lang, "adm.denied"))
        return
    await send_long(message.bot, message.chat.id, await _menu_text(services, lang), reply_markup=admin_menu(lang))


@router.callback_query(AdminCB.filter(F.action == "menu"))
async def back_to_menu(callback: CallbackQuery, lang: str, services: Services, is_admin: bool = False) -> None:
    if not is_admin:
        await callback.answer("⛔", show_alert=True)
        return
    await edit_or_send(callback, await _menu_text(services, lang), reply_markup=admin_menu(lang))


# ═══════════════════════════════════════════════════════════════════════════
#  statistics
# ═══════════════════════════════════════════════════════════════════════════
@router.callback_query(AdminCB.filter(F.action == "stats"))
async def statistics(callback: CallbackQuery, lang: str, services: Services, is_admin: bool = False) -> None:
    if not is_admin:
        await callback.answer("⛔", show_alert=True)
        return
    ar = lang.startswith("ar")
    stats = await services.repos.users.activity_stats()
    usage = await services.usage.platform(days=7)
    totals = await services.repos.economy.totals()
    economy_rows = sorted(totals.items(), key=lambda kv: -abs(kv[1]["points"]))[:8]
    economy = "\n".join(
        f"• {escape_html(str(reason))}: {row['points']:+} "
        f"{'نقطة' if ar else 'pts'} ({row['count']} "
        f"{'حركة' if ar else 'entries'})"
        for reason, row in economy_rows
    ) or "—"
    cost = "\n".join([
        f"{'اليوم' if ar else 'Today'}: ${usage['today_cost_usd']:.4f} / ${settings.global_daily_usd_cap:.2f}",
        f"{'7 أيام' if ar else '7 days'}: ${usage['total_cost_usd']:.4f} · "
        f"{usage['calls']} {'استدعاء' if ar else 'calls'} · {usage['errors']} "
        f"{'خطأ/تجاوز' if ar else 'errors'}",
        f"{'توكنز' if ar else 'Tokens'}: {compact_number(sum(r['tokens'] for r in usage['by_model']))}",
    ])
    await edit_or_send(
        callback,
        t(lang, "adm.stats_body", total=stats["total"], today=stats["today"], week=stats["week"],
          month=stats["month"], onboarded=stats["onboarded"], coaches=stats["coaches"],
          banned=stats["banned"], economy=economy, cost=cost),
        reply_markup=admin_menu(lang),
    )


# ═══════════════════════════════════════════════════════════════════════════
#  API usage & configuration
# ═══════════════════════════════════════════════════════════════════════════
@router.callback_query(AdminCB.filter(F.action == "ai"))
async def api_usage(callback: CallbackQuery, lang: str, services: Services, is_admin: bool = False) -> None:
    if not is_admin:
        await callback.answer("⛔", show_alert=True)
        return
    data = await services.usage.platform(days=7)
    await edit_or_send(callback, services.usage.format_admin(data, lang), reply_markup=admin_config(lang))


@router.callback_query(AdminCB.filter(F.action == "cfg_models"))
async def cfg_models(callback: CallbackQuery, lang: str, services: Services, is_admin: bool = False) -> None:
    if not is_admin:
        await callback.answer("⛔", show_alert=True)
        return
    ar = lang.startswith("ar")
    live_router = services.ai.router
    await live_router.load_overrides(force=True)
    lines = [f"<b>🧩 {'توزيع الموديلات الحي' if ar else 'Live model routing'}</b>"]
    for task, models in live_router.describe().items():
        chain = " → ".join(f"<code>{escape_html(m)}</code>" for m in models)
        price_in, price_out = settings.price_of(models[0]) if models else (0.0, 0.0)
        lines.append(f"• {task}: {chain}\n   ${price_in}/M "
                     f"{'إدخال' if ar else 'in'} · ${price_out}/M "
                     f"{'إخراج' if ar else 'out'}")
    tasks = ", ".join(f"<code>{task.value}</code>" for task in AITask)
    lines += [
        "",
        ("لتعديل موديل: <code>/route {task} {model}</code>\nلإلغاء التعديلات: <code>/route reset</code>"
         if ar else "Change a model: <code>/route {task} {model}</code>\nReset: <code>/route reset</code>"),
        f"{'المهام' if ar else 'Tasks'}: {tasks}",
    ]
    await edit_or_send(callback, "\n".join(lines), reply_markup=admin_config(lang))


@router.callback_query(AdminCB.filter(F.action == "cfg_points"))
async def cfg_points(callback: CallbackQuery, lang: str, is_admin: bool = False) -> None:
    if not is_admin:
        await callback.answer("⛔", show_alert=True)
        return
    ar = lang.startswith("ar")
    lines = [
        f"<b>💵 {'اقتصاد النقاط' if ar else 'Points economy'}</b>",
        f"1 USD = {settings.points_per_usd} "
        f"{'نقطة' if ar else 'points'}",
        f"🎁 {'مكافأة الترحيب' if ar else 'Welcome bonus'}: {settings.welcome_bonus_points}",
        f"🏆 {'كوتش 24 ساعة' if ar else '24h coach'}: {settings.coach_access_cost_points} "
        f"{'نقطة /' if ar else 'pts /'} {settings.coach_access_days} "
        f"{'يوم' if ar else 'days'}",
        f"💪 {'رسالة كوتش' if ar else 'Coach message'}: {settings.coach_points_minimum}–"
        f"{settings.coach_points_maximum} "
        f"{'نقطة (حسب التوكنز)' if ar else 'pts (token-metered)'}",
        f"🍽️ {'صورة أكل' if ar else 'Food photo'}: {settings.food_scan_free_daily} "
        f"{'مجانًا ثم' if ar else 'free then'} {settings.food_scan_cost_points}",
        f"📸 {'صورة تقدم' if ar else 'Progress photo'}: {settings.progress_scan_cost_points}",
        f"📄 {'تصدير' if ar else 'Export'}: {settings.report_export_cost_points}",
        f"🎁 {'إحالة' if ar else 'Referral'}: +{settings.referral_inviter_points} / "
        f"+{settings.referral_invitee_points}"
        + (f" · {'مكافأة كل' if ar else 'bonus every'} {settings.referral_bonus_every}: "
           f"+{settings.referral_bonus_points}" if settings.referral_bonus_every else ""),
        f"💎 {'اشتراك شهري' if ar else 'Monthly plan'}: +{settings.monthly_plan_points} · "
        f"{'ربع سنوي' if ar else 'Quarterly'}: +{settings.quarterly_plan_points}",
        f"🔥 {'جوائز الالتزام' if ar else 'Streak rewards'}: +{settings.streak_reward_points} "
        f"({', '.join(str(m) for m in settings.streak_milestones)})",
    ]
    await edit_or_send(callback, "\n".join(lines), reply_markup=admin_config(lang))


@router.callback_query(AdminCB.filter(F.action == "cfg_caps"))
async def cfg_caps(callback: CallbackQuery, lang: str, services: Services, is_admin: bool = False) -> None:
    if not is_admin:
        await callback.answer("⛔", show_alert=True)
        return
    ar = lang.startswith("ar")
    live_cap = await services.repos.settings.get(ai_router.BUDGET_KEY, settings.global_daily_usd_cap)
    usage = await services.usage.platform(days=1)
    lines = [
        f"<b>🛑 {'حدود الاستخدام' if ar else 'Usage caps'}</b>",
        f"💵 {'السقف اليومي العالمي' if ar else 'Global daily cap'}: ${float(live_cap):.2f} "
        f"({'مستهلك' if ar else 'used'} ${usage['today_cost_usd']:.3f} — {usage['cap_used_pct']}%) · "
        f"{'الإجراء' if ar else 'action'}: <code>{settings.cap_action}</code>",
        f"👤 {'نقاط المستخدم/يوم' if ar else 'User points/day'}: {settings.user_daily_points_cap} · "
        f"{'شهر' if ar else 'month'}: {settings.user_monthly_points_cap}",
        f"💬 {'استشارات مجانية/يوم' if ar else 'Free consults/day'}: {settings.consult_free_daily_limit}",
        f"🏆 {'رسائل كوتش/يوم' if ar else 'Coach messages/day'}: {settings.coach_daily_message_limit}",
        f"👁️ {'تحليل صور/يوم' if ar else 'Vision calls/day'}: {settings.vision_daily_limit}",
        "",
        ("لتعديل السقف العالمي: <code>/cap 15</code>" if ar else "Change the global cap: <code>/cap 15</code>"),
    ]
    await edit_or_send(callback, "\n".join(lines), reply_markup=admin_config(lang))


@router.callback_query(AdminCB.filter(F.action == "subs_run"))
async def run_subscriptions(
    callback: CallbackQuery, lang: str, services: Services, is_admin: bool = False
) -> None:
    if not is_admin:
        await callback.answer("⛔", show_alert=True)
        return
    ar = lang.startswith("ar")
    results = await services.subscription.run_renewals()
    lines = [f"<b>💎 {'تجديد الاشتراكات' if ar else 'Subscription renewals'}</b>"]
    if not results:
        lines.append("—" if not ar else "لا يوجد اشتراكات مستحقة الآن.")
    for item in results:
        lines.append(
            f"• <code>{item.tg_id}</code> {'+' if item.ok else '✖'}{item.points} "
            + ("" if item.ok else f" — {escape_html(str(item.error)[:60])}")
        )
    await edit_or_send(callback, "\n".join(lines), reply_markup=admin_config(lang))


@router.message(Command("cap"))
async def set_cap(message: Message, user: User, lang: str, services: Services, is_admin: bool = False) -> None:
    if not is_admin:
        return
    parts = (message.text or "").split(maxsplit=1)
    value = parse_float(parts[1]) if len(parts) > 1 else None
    if value is None or value < 0:
        await message.answer("الاستخدام: <code>/cap 15</code>")
        return
    await services.repos.settings.set(ai_router.BUDGET_KEY, value, updated_by=user.tg_id)
    ai_router.invalidate_cache()
    await services.repos.audit.log(user.tg_id, AdminActionEnum.CONFIG_CHANGE.value,
                                   payload={"global_daily_usd_cap": value})
    await message.answer(f"✅ {'السقف اليومي العالمي صار' if lang.startswith('ar') else 'Global daily cap is now'} "
                         f"${value:.2f}")


@router.message(Command("route"))
async def set_route(message: Message, user: User, lang: str, services: Services, is_admin: bool = False) -> None:
    if not is_admin:
        return
    ar = lang.startswith("ar")
    parts = (message.text or "").split()
    if len(parts) == 2 and parts[1] == "reset":
        await services.repos.settings.set(ai_router.ROUTING_KEY, {}, updated_by=user.tg_id)
        ai_router.invalidate_cache()
        await message.answer("✅ " + ("أُلغيت كل تعديلات التوجيه." if ar else "All routing overrides cleared."))
        return
    if len(parts) < 3:
        tasks = ", ".join(f"<code>{task.value}</code>" for task in AITask)
        await message.answer(f"{'الاستخدام' if ar else 'Usage'}: <code>/route task model [fallback…]</code>\n"
                             f"{'المهام' if ar else 'Tasks'}: {tasks}")
        return
    task, models = parts[1].lower(), parts[2:]
    if task not in {item.value for item in AITask}:
        await message.answer("❌ " + ("مهمة غير معروفة." if ar else "Unknown task."))
        return
    current = await services.repos.settings.get(ai_router.ROUTING_KEY, {}) or {}
    if not isinstance(current, dict):
        current = {}
    current[task] = models
    await services.repos.settings.set(ai_router.ROUTING_KEY, current, updated_by=user.tg_id)
    services.ai.router.set_override(task, models)
    ai_router.invalidate_cache()
    await services.repos.audit.log(user.tg_id, AdminActionEnum.CONFIG_CHANGE.value,
                                   payload={"model_routing": {task: models}})
    await message.answer(f"✅ {task} → <code>{escape_html(' → '.join(models))}</code>")


# ═══════════════════════════════════════════════════════════════════════════
#  users
# ═══════════════════════════════════════════════════════════════════════════
def _user_line(person: User, lang: str) -> str:
    ar = lang.startswith("ar")
    flags = ""
    if person.is_banned:
        flags += "⛔"
    if person.has_coach_access:
        flags += "🏆"
    if person.has_subscription:
        flags += "💎"
    if not person.onboarding_done:
        flags += "⏳"
    return (
        f"• {escape_html(person.full_name)} — <code>{person.tg_id}</code> · "
        f"{int(person.points_balance or 0)} {'نقطة' if ar else 'pts'} · "
        f"{person.weight_kg or '?'} "
        f"{'كجم' if ar else 'kg'} · {get_goal(person.goal).describe(lang)[:24]} {flags}"
    )


def _users_keyboard(lang: str, people: Sequence[User], page: int, total_pages: int):
    builder = InlineKeyboardBuilder()
    for person in people[:PER_PAGE]:
        builder.row(InlineKeyboardButton(
            text=f"👤 {person.full_name[:26]}",
            callback_data=AdminUserCB(action="profile", user_id=str(person.id)).pack(),
        ))
    pager = admin_users_pager(lang, page=page, total_pages=total_pages)
    for row in pager.inline_keyboard:
        builder.row(*row)
    return builder.as_markup()


@router.callback_query(AdminCB.filter(F.action.in_({"users", "users_goal"})))
async def users_list(
    callback: CallbackQuery, callback_data: AdminCB, lang: str, services: Services, is_admin: bool = False
) -> None:
    if not is_admin:
        await callback.answer("⛔", show_alert=True)
        return
    page = max(1, callback_data.page or 1)
    goal = callback_data.arg if callback_data.action == "users_goal" and callback_data.arg else None
    people, total = await services.repos.users.paginate(page=page, per_page=PER_PAGE, goal=goal)
    total_pages = max(1, -(-total // PER_PAGE))
    ar = lang.startswith("ar")
    header = f"<b>👥 {'المستخدمون' if ar else 'Users'} ({total})</b>"
    if goal:
        header += f" — 🎯 {get_goal(goal).describe(lang)}"
    lines = [header] + [_user_line(person, lang) for person in people]
    if not people:
        lines.append("—")
    await edit_or_send(callback, "\n".join(lines),
                       reply_markup=_users_keyboard(lang, people, page, total_pages))


@router.callback_query(AdminCB.filter(F.action == "goal_filter"))
async def goal_filter(callback: CallbackQuery, lang: str, is_admin: bool = False) -> None:
    if not is_admin:
        await callback.answer("⛔", show_alert=True)
        return
    await edit_or_send(callback, "🎯 " + ("اختر هدفًا للتصفية:" if lang.startswith("ar") else "Filter by goal:"),
                       reply_markup=admin_goal_filter(lang))


@router.callback_query(AdminCB.filter(F.action == "search"))
async def search_prompt(callback: CallbackQuery, lang: str, state: FSMContext, is_admin: bool = False) -> None:
    if not is_admin:
        await callback.answer("⛔", show_alert=True)
        return
    await state.set_state(AdminFlow.search)
    await callback.answer()
    await callback.message.answer(t(lang, "adm.search_ask"), reply_markup=remove_keyboard())


@router.message(AdminFlow.search, F.text)
async def do_search(
    message: Message, lang: str, services: Services, state: FSMContext, is_admin: bool = False
) -> None:
    if not is_admin:
        return
    await state.clear()
    query = normalize_digits((message.text or "").strip())
    if not query:
        return
    people = await services.repos.users.search(query, limit=PER_PAGE)
    if not people:
        await message.answer("🔎 " + ("لا نتائج." if lang.startswith("ar") else "No results."),
                             reply_markup=admin_menu(lang))
        return
    lines = [f"🔎 {len(people)} " + ("نتيجة" if lang.startswith("ar") else "results")]
    lines += [_user_line(person, lang) for person in people]
    builder = InlineKeyboardBuilder()
    for person in people[:PER_PAGE]:
        builder.row(InlineKeyboardButton(
            text=f"👤 {person.full_name[:26]}",
            callback_data=AdminUserCB(action="profile", user_id=str(person.id)).pack(),
        ))
    for row in admin_menu(lang).inline_keyboard:
        builder.row(*row)
    await send_long(message.bot, message.chat.id, "\n".join(lines), reply_markup=builder.as_markup())


async def _load_person(services: Services, raw_id: str) -> User | None:
    try:
        return await services.repos.users.by_id(uuid.UUID(raw_id))
    except (ValueError, AttributeError):
        return None


@router.callback_query(AdminUserCB.filter(F.action.in_({"profile", "open"})))
async def open_user(
    callback: CallbackQuery, callback_data: AdminUserCB, lang: str, services: Services, is_admin: bool = False
) -> None:
    if not is_admin:
        await callback.answer("⛔", show_alert=True)
        return
    person = await _load_person(services, callback_data.user_id)
    if person is None:
        await callback.answer("❌", show_alert=True)
        return
    ar = lang.startswith("ar")
    log = await services.repos.tracking.daily_log(person, person.local_today())
    await services.repos.audit.log(callback.from_user.id, AdminActionEnum.VIEW_PROFILE.value,
                                   target_user_id=person.id)
    text = t(
        lang, "adm.user_card",
        name=escape_html(person.full_name), tg_id=person.tg_id,
        points=int(person.points_balance or 0),
        earned=int(person.total_points_earned or 0), spent=int(person.total_points_spent or 0),
        goal=get_goal(person.goal).describe(lang), equipment=person.equipment or "—",
        age=person.age or "—", height=person.height_cm or "—", weight=person.weight_kg or "—",
        calories=int(log.target_calories or 0), protein=int(log.target_protein_g or 0),
        streak=int(person.streak_current or 0), best=int(person.streak_best or 0),
        workouts=int(person.total_workouts or 0),
        coach=(person.coach_access_until.strftime("%Y-%m-%d") if person.has_coach_access
               else ("مفتوح دائمًا" if person.coach_unlocked_forever else "—")),
        sub=subscription_label(person.subscription_plan or "none", lang),
        active=(person.last_active_at.strftime("%Y-%m-%d %H:%M") if person.last_active_at
                else ("—" if not ar else "—")),
        status=("⛔ محظور" if person.is_banned else ("✅ مكتمل" if person.onboarding_done else "⏳ ناقص")),
    )
    if person.username:
        text += f"\n@{escape_html(person.username)}"
    text += f"\n{'كود الإحالة' if ar else 'Referral code'}: <code>{person.referral_code}</code> · " \
            f"{'دعوات' if ar else 'refs'}: {person.referrals_count}"
    await edit_or_send(callback, text,
                       reply_markup=admin_user_card(lang, str(person.id), is_banned=bool(person.is_banned)))


@router.callback_query(AdminUserCB.filter(F.action.in_({"ban", "unban"})))
async def toggle_ban(
    callback: CallbackQuery, callback_data: AdminUserCB, lang: str, services: Services, is_admin: bool = False
) -> None:
    if not is_admin:
        await callback.answer("⛔", show_alert=True)
        return
    person = await _load_person(services, callback_data.user_id)
    if person is None:
        await callback.answer("❌", show_alert=True)
        return
    banned = callback_data.action == "ban"
    reason = callback_data.arg or None
    await services.repos.users.set_ban(person, banned=banned, reason=reason)
    await services.repos.audit.log(
        callback.from_user.id,
        (AdminActionEnum.BAN if banned else AdminActionEnum.UNBAN).value,
        target_user_id=person.id, payload={"reason": reason},
    )
    await callback.answer(t(lang, "adm.banned") if banned else t(lang, "adm.unbanned"))
    await open_user(callback, callback_data, lang, services, is_admin)


@router.callback_query(AdminUserCB.filter(F.action.in_({"grant", "deduct"})))
async def points_prompt(
    callback: CallbackQuery, callback_data: AdminUserCB, lang: str, state: FSMContext, is_admin: bool = False
) -> None:
    if not is_admin:
        await callback.answer("⛔", show_alert=True)
        return
    await state.set_state(AdminFlow.amount)
    await state.update_data(target_user=callback_data.user_id, direction=callback_data.action)
    await callback.answer()
    await callback.message.answer(
        t(lang, "adm.amount_ask") + "\n<code>500 "
        + ("شحن اشتراك</code>" if lang.startswith("ar") else "monthly top-up</code>"),
        reply_markup=remove_keyboard(),
    )


@router.message(AdminFlow.amount, F.text)
async def points_apply(
    message: Message, lang: str, services: Services, state: FSMContext, is_admin: bool = False
) -> None:
    if not is_admin:
        return
    ar = lang.startswith("ar")
    data = await state.get_data()
    target_id, direction = data.get("target_user"), data.get("direction") or "grant"
    parts = normalize_digits((message.text or "").strip()).split(maxsplit=1)
    amount = parse_int(parts[0]) if parts else None
    note = safe_str(parts[1] if len(parts) > 1 else None, 200) or (
        "شحن يدوي" if ar else "manual top-up")
    person = await _load_person(services, target_id or "")
    if not amount or amount <= 0 or person is None:
        await message.answer(t(lang, "adm.amount_ask"))
        return
    try:
        if direction == "deduct":
            charge = await services.points.admin_deduct(person, amount, admin_tg_id=message.from_user.id,
                                                        note=note)
        else:
            charge = await services.points.admin_grant(person, amount, admin_tg_id=message.from_user.id,
                                                       note=note)
    except InsufficientPoints as exc:
        await message.answer(f"❌ {'الرصيد لا يكفي' if ar else 'Balance too low'} "
                             f"({exc.available}).")
        return
    await services.repos.audit.log(
        message.from_user.id,
        (AdminActionEnum.DEDUCT_POINTS if direction == "deduct" else AdminActionEnum.GRANT_POINTS).value,
        target_user_id=person.id, payload={"amount": amount, "note": note},
    )
    await state.clear()
    await message.answer(t(lang, "adm.points_done", balance=charge.balance)
                         + f"\n<code>{person.tg_id}</code> · {sign_of(direction)}{amount}")
    await notify(
        message.bot, person.tg_id,
        ("🪙 عدّلت الإدارة رصيدك: " if ar else "🪙 The admin adjusted your balance: ")
        + f"{sign_of(direction)}{amount} → {charge.balance} "
        + ("نقطة" if ar else "points") + (f" ({escape_html(note)})" if note else ""),
    )


def sign_of(direction: str) -> str:
    return "−" if direction == "deduct" else "+"


@router.callback_query(AdminUserCB.filter(F.action == "sub"))
async def grant_subscription(
    callback: CallbackQuery, callback_data: AdminUserCB, lang: str, services: Services, is_admin: bool = False
) -> None:
    if not is_admin:
        await callback.answer("⛔", show_alert=True)
        return
    person = await _load_person(services, callback_data.user_id)
    if person is None:
        await callback.answer("❌", show_alert=True)
        return
    plan = callback_data.arg if callback_data.arg in {p.value for p in SubscriptionPlan} else SubscriptionPlan.MONTHLY.value
    charge = await services.subscription.activate(person, plan, admin_tg_id=callback.from_user.id)
    await services.repos.audit.log(callback.from_user.id, AdminActionEnum.SET_SUBSCRIPTION.value,
                                   target_user_id=person.id, payload={"plan": plan})
    await callback.answer(t(lang, "adm.points_done", balance=charge.balance if charge else
                            int(person.points_balance or 0)))
    await notify(
        callback.bot, person.tg_id,
        f"💎 {'تم تفعيل اشتراكك' if lang.startswith('ar') else 'Your subscription is active'}: "
        f"{subscription_label(plan, lang)}"
        + (f" (+{charge.points} {'نقطة' if lang.startswith('ar') else 'points'})" if charge else ""),
    )
    await open_user(callback, callback_data, lang, services, is_admin)


@router.callback_query(AdminUserCB.filter(F.action == "usage"))
async def user_usage(
    callback: CallbackQuery, callback_data: AdminUserCB, lang: str, services: Services, is_admin: bool = False
) -> None:
    if not is_admin:
        await callback.answer("⛔", show_alert=True)
        return
    person = await _load_person(services, callback_data.user_id)
    if person is None:
        await callback.answer("❌", show_alert=True)
        return
    ar = lang.startswith("ar")
    data = await services.usage.for_user(person, days=30)
    lines = [
        f"<b>🤖 {escape_html(person.full_name)} — {'استهلاكه' if ar else 'usage'}</b>",
        f"{'التكلفة (30 يوم)' if ar else 'Cost (30d)'}: ${float(data.get('cost_usd', 0)):.4f} · "
        f"{'استدعاءات' if ar else 'calls'}: {data.get('calls', 0)} · "
        f"{'توكنز' if ar else 'tokens'}: {compact_number(int(data.get('tokens', 0)))}",
        f"{'نقاط' if ar else 'Points'}: {int(person.points_balance or 0)} · "
        f"{'اليوم' if ar else 'today'}: {data.get('daily_points_spent', 0)}/{settings.user_daily_points_cap} · "
        f"{'الشهر' if ar else 'month'}: {data.get('monthly_points_spent', 0)}/"
        f"{settings.user_monthly_points_cap}",
        f"💬 {data.get('daily_consult_count', 0)}/{settings.consult_free_daily_limit} · "
        f"🏆 {data.get('daily_coach_messages', 0)}/{settings.coach_daily_message_limit} · "
        f"👁️ {data.get('daily_vision_count', 0)}/{settings.vision_daily_limit}",
    ]
    await edit_or_send(callback, "\n".join(lines),
                       reply_markup=admin_user_card(lang, str(person.id), is_banned=bool(person.is_banned)))


@router.callback_query(AdminUserCB.filter(F.action == "ledger"))
async def user_ledger(
    callback: CallbackQuery, callback_data: AdminUserCB, lang: str, services: Services, is_admin: bool = False
) -> None:
    if not is_admin:
        await callback.answer("⛔", show_alert=True)
        return
    person = await _load_person(services, callback_data.user_id)
    if person is None:
        await callback.answer("❌", show_alert=True)
        return
    ar = lang.startswith("ar")
    page = max(0, callback_data.page)
    entries = await services.repos.economy.ledger(person.id, limit=PER_PAGE, offset=page * PER_PAGE)
    total = await services.repos.economy.ledger_count(person.id)
    lines = [f"<b>📒 {escape_html(person.full_name)} — {'سجل نقاطه' if ar else 'ledger'} ({total})</b>"]
    for entry in entries:
        stamp = entry.created_at.strftime("%m-%d %H:%M") if entry.created_at else ""
        sign = "+" if entry.amount > 0 else "−"
        lines.append(f"{stamp} {sign}<b>{abs(int(entry.amount))}</b> · "
                     f"{escape_html(str(entry.reason))}"
                     + (f" — {escape_html(str(entry.note))}" if entry.note else ""))
    if not entries:
        lines.append("—")
    builder = InlineKeyboardBuilder()
    if page > 0:
        builder.row(InlineKeyboardButton(text="⬅️", callback_data=AdminUserCB(
            action="ledger", user_id=callback_data.user_id, page=page - 1).pack()))
    builder.row(InlineKeyboardButton(text=f"{page + 1}/{max(1, -(-total // PER_PAGE))}",
                                     callback_data=AdminUserCB(action="ledger", user_id=callback_data.user_id,
                                                               page=page).pack()))
    if (page + 1) * PER_PAGE < total:
        builder.row(InlineKeyboardButton(text="➡️", callback_data=AdminUserCB(
            action="ledger", user_id=callback_data.user_id, page=page + 1).pack()))
    builder.row(InlineKeyboardButton(text=t(lang, "adm.view_profile"), callback_data=AdminUserCB(
        action="profile", user_id=callback_data.user_id).pack()))
    await edit_or_send(callback, "\n".join(lines), reply_markup=builder.as_markup())


# ═══════════════════════════════════════════════════════════════════════════
#  broadcasts
# ═══════════════════════════════════════════════════════════════════════════
@router.callback_query(AdminCB.filter(F.action == "bc"))
async def broadcast_start(callback: CallbackQuery, lang: str, state: FSMContext, is_admin: bool = False) -> None:
    if not is_admin:
        await callback.answer("⛔", show_alert=True)
        return
    await state.set_state(AdminFlow.broadcast)
    await callback.answer()
    await callback.message.answer(t(lang, "adm.bc_ask", audience="all"), reply_markup=remove_keyboard())


@router.message(AdminFlow.broadcast, (F.text | F.photo))
async def broadcast_text(
    message: Message, lang: str, services: Services, state: FSMContext, is_admin: bool = False
) -> None:
    if not is_admin:
        return
    text = message.text or message.caption or ""
    if len(text.strip()) < 3:
        await message.answer(t(lang, "common.error"))
        return
    media_file_id = message.photo[-1].file_id if message.photo else None
    await state.update_data(bc_text=text, bc_media=media_file_id)
    await state.set_state(AdminFlow.note)     # note = waiting for the audience choice
    counts = {
        audience: len(await services.repos.users.broadcast_audience(audience)) for audience in AUDIENCES
    }
    ar = lang.startswith("ar")
    summary = " · ".join(f"{audience}={counts[audience]}" for audience in AUDIENCES)
    await message.answer(
        f"👥 {'اختر الجمهور' if ar else 'Choose the audience'} ({summary})\n"
        f"{'أو اكتب «الآن» للإرسال الفوري للكل.' if ar else 'Or type “now” to send to everyone.'}",
        reply_markup=admin_broadcast(lang),
    )


@router.callback_query(AdminCB.filter(F.action == "bc_set"))
async def broadcast_audience(
    callback: CallbackQuery, callback_data: AdminCB, lang: str, services: Services, state: FSMContext,
    is_admin: bool = False,
) -> None:
    if not is_admin:
        await callback.answer("⛔", show_alert=True)
        return
    data = await state.get_data()
    audience = callback_data.arg if callback_data.arg in AUDIENCES else "all"
    text = data.get("bc_text")
    if not text:
        await state.clear()
        await callback.answer(t(lang, "common.error"), show_alert=True)
        return
    await state.clear()
    broadcaster = services.broadcaster(callback.bot)
    broadcast_id, total = await broadcaster.create(
        admin_tg_id=callback.from_user.id, text=text, media_file_id=data.get("bc_media"),
        audience=audience,
    )
    await callback.answer(t(lang, "adm.bc_started", total=total))
    await edit_or_send(
        callback,
        t(lang, "adm.bc_started", total=total)
        + f"\n<code>{broadcast_id}</code> · {escape_html(audience)}\n"
        + ("سيبدأ الإرسال خلال دقيقة عبر المجدول." if lang.startswith("ar")
           else "Delivery starts within a minute via the scheduler."),
        reply_markup=admin_menu(lang),
    )


@router.message(AdminFlow.note, F.text)
async def broadcast_now(
    message: Message, lang: str, services: Services, state: FSMContext, is_admin: bool = False
) -> None:
    """Typing “now”/“الآن” sends to everyone immediately; a date schedules it."""
    if not is_admin:
        return
    data = await state.get_data()
    text = data.get("bc_text")
    if not text:
        await state.clear()
        return
    when = _parse_when(message.text or "")
    raw = (message.text or "").strip()
    lowered = raw.lower()
    if lowered in AUDIENCES:
        audience, when = lowered, None
    elif _is_now(raw):
        audience, when = "all", None
    elif when is not None:
        audience = "all"
    else:
        ar = lang.startswith("ar")
        await message.answer(
            "⏰ " + ("اكتب «الآن»، أو اسم الجمهور (all/active/coaches/onboarded)، "
                     "أو وقتًا مثل <code>2026-09-03 18:00</code> أو <code>+2h</code>"
                     if ar
                     else "Type “now”, an audience (all/active/coaches/onboarded), "
                          "or a time like <code>2026-09-03 18:00</code> / <code>+2h</code>")
        )
        return

    await state.clear()
    broadcaster = services.broadcaster(message.bot)
    broadcast_id, total = await broadcaster.create(
        admin_tg_id=message.from_user.id, text=text, media_file_id=data.get("bc_media"),
        audience=audience, scheduled_for=when,
    )
    ar = lang.startswith("ar")
    if when is None:
        await message.answer(t(lang, "adm.bc_started", total=total)
                             + f"\n<code>{broadcast_id}</code> · {escape_html(audience)}",
                             reply_markup=admin_menu(lang))
    else:
        await message.answer(
            (f"⏰ جُدول الإرسال <code>{broadcast_id}</code> إلى {total} مستخدم في {when:%Y-%m-%d %H:%M}."
             if ar
             else f"⏰ Scheduled <code>{broadcast_id}</code> to {total} users at {when:%Y-%m-%d %H:%M}."),
            reply_markup=admin_menu(lang),
        )


@router.callback_query(AdminCB.filter(F.action == "bc_history"))
async def broadcast_history(
    callback: CallbackQuery, lang: str, services: Services, is_admin: bool = False
) -> None:
    if not is_admin:
        await callback.answer("⛔", show_alert=True)
        return
    ar = lang.startswith("ar")
    rows = await services.broadcaster(callback.bot).history(limit=10)
    lines = [f"<b>📨 {'الإرساليات' if ar else 'Broadcasts'}</b>"]
    for row in rows:
        stamp = (row.get("created_at") or "")[:16].replace("T", " ")
        lines.append(
            f"• {stamp} <code>{str(row['id'])[:8]}</code> {row['status']} — "
            f"{row['sent']}/{row['total']} "
            f"({row['failed']} {'فشل' if ar else 'failed'}) · {escape_html(row['text'])}"
        )
    if not rows:
        lines.append("—")
    await edit_or_send(callback, "\n".join(lines), reply_markup=admin_broadcast(lang))


def _is_now(raw: str) -> bool:
    return normalize_digits(raw.strip().lower()) in {"now", "الآن", "الان", "all", "send"}


def _parse_when(raw: str) -> datetime | None:
    value = normalize_digits((raw or "").strip().lower())
    if _is_now(value) or not value:
        return None
    if value.startswith("+"):
        unit = value[-1]
        number = parse_float(value[1:-1]) or 0.0
        delta = {"h": timedelta(hours=number), "d": timedelta(days=number),
                 "m": timedelta(minutes=number)}.get(unit, timedelta(hours=number))
        return datetime.now() + delta
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d", "%d/%m/%Y %H:%M", "%H:%M"):
        try:
            parsed = datetime.strptime(value, fmt)
        except ValueError:
            continue
        if fmt == "%H:%M":
            today = datetime.now()
            parsed = parsed.replace(year=today.year, month=today.month, day=today.day)
            if parsed < today:
                parsed += timedelta(days=1)
        return parsed
    return None


# ═══════════════════════════════════════════════════════════════════════════
#  forced subscription
# ═══════════════════════════════════════════════════════════════════════════
@router.callback_query(AdminCB.filter(F.action == "channels"))
async def channels_panel(
    callback: CallbackQuery, lang: str, services: Services, is_admin: bool = False
) -> None:
    if not is_admin:
        await callback.answer("⛔", show_alert=True)
        return
    from app.middlewares.auth import _forced_channels

    ar = lang.startswith("ar")
    current = await _forced_channels(services)
    state = ("🟢 " + ("مفعّل" if ar else "on")) if current else ("⚪️ " + ("متوقف" if ar else "off"))
    text = t(
        lang, "adm.channels_body",
        state=state,
        channels="\n".join(f"• <code>{escape_html(c)}</code>" for c in current) or "—",
    )
    text += "\n\n" + (
        "اكتب قائمة القنوات (سطر لكل قناة: <code>@channel</code> أو <code>-1001234567890</code>)، "
        "أو <code>clear</code> للمسح."
        if ar else
        "Send the channel list (one per line: <code>@channel</code> or <code>-1001234567890</code>), "
        "or <code>clear</code>."
    )
    await edit_or_send(callback, text,
                       reply_markup=admin_channels(lang, enabled=bool(current)))


@router.callback_query(AdminCB.filter(F.action == "channels_toggle"))
async def channels_toggle(
    callback: CallbackQuery, user: User, lang: str, services: Services, is_admin: bool = False
) -> None:
    if not is_admin:
        await callback.answer("⛔", show_alert=True)
        return
    from app.middlewares.auth import _forced_channels

    current = await _forced_channels(services)
    new_state = not current
    await services.repos.settings.set("forced_channels_enabled", new_state, updated_by=user.tg_id)
    await services.repos.audit.log(user.tg_id, AdminActionEnum.CONFIG_CHANGE.value,
                                   payload={"forced_channels_enabled": new_state})
    ai_router.invalidate_cache()
    await callback.answer("✅")
    await channels_panel(callback, lang, services, is_admin)


@router.callback_query(AdminCB.filter(F.action == "channels_edit"))
async def channels_edit(callback: CallbackQuery, lang: str, state: FSMContext, is_admin: bool = False) -> None:
    if not is_admin:
        await callback.answer("⛔", show_alert=True)
        return
    await state.set_state(AdminFlow.channels)
    await callback.answer()
    ar = lang.startswith("ar")
    await callback.message.answer(
        "اكتب القنوات (سطر لكل قناة) أو <code>clear</code>:" if ar else "Send channels (one per line) or <code>clear</code>:",
        reply_markup=remove_keyboard(),
    )


@router.message(AdminFlow.channels, F.text)
async def channels_apply(
    message: Message, user: User, lang: str, services: Services, state: FSMContext, is_admin: bool = False
) -> None:
    if not is_admin:
        return
    await state.clear()
    raw = (message.text or "").strip()
    if raw.lower() in {"clear", "مسح", "حذف"}:
        channels: list[str] = []
    else:
        channels = [line.strip() for line in raw.splitlines() if line.strip()][:10]
    await services.repos.settings.set("forced_channels", channels, updated_by=user.tg_id)
    if channels:
        await services.repos.settings.set("forced_channels_enabled", True, updated_by=user.tg_id)
    await services.repos.audit.log(user.tg_id, AdminActionEnum.CONFIG_CHANGE.value,
                                   payload={"forced_channels": channels})
    ai_router.invalidate_cache()
    await message.answer("✅ " + (", ".join(escape_html(c) for c in channels) or "—"),
                         reply_markup=admin_channels(lang, enabled=bool(channels)))


# ═══════════════════════════════════════════════════════════════════════════
#  audit log & CSV export
# ═══════════════════════════════════════════════════════════════════════════
@router.callback_query(AdminCB.filter(F.action == "audit"))
async def audit_panel(callback: CallbackQuery, lang: str, services: Services, is_admin: bool = False) -> None:
    if not is_admin:
        await callback.answer("⛔", show_alert=True)
        return
    entries = await services.repos.audit.recent(limit=20)
    rows = "\n".join(
        f"• {(entry.created_at.strftime('%m-%d %H:%M') if entry.created_at else '')} "
        f"{mask_id(entry.admin_tg_id or 0)} · <code>{escape_html(entry.action)}</code>"
        for entry in entries
    ) or "—"
    await edit_or_send(callback, t(lang, "adm.audit_body", rows=rows), reply_markup=admin_menu(lang))


@router.message(Command("export"))
async def export_csv(message: Message, lang: str, services: Services, is_admin: bool = False) -> None:
    if not is_admin:
        return
    people, _total = await services.repos.users.paginate(page=1, per_page=5000)
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["tg_id", "name", "username", "lang", "points", "streak", "weight", "goal",
                     "onboarded", "coach_until", "subscription", "referrals", "joined", "last_active"])
    for person in people:
        writer.writerow([
            person.tg_id, person.full_name, person.username or "", person.language_code or "",
            int(person.points_balance or 0), person.streak_current, person.weight_kg, person.goal,
            int(person.onboarding_done),
            person.coach_access_until.isoformat() if person.coach_access_until else "",
            person.subscription_plan or "", person.referrals_count,
            person.created_at.strftime("%Y-%m-%d") if person.created_at else "",
            person.last_active_at.strftime("%Y-%m-%d %H:%M") if person.last_active_at else "",
        ])
    await send_document_bytes(
        message.bot, message.chat.id, buffer.getvalue().encode("utf-8-sig"),
        filename=f"users-{datetime.now():%Y%m%d}.csv", caption="📄 CSV",
    )


@router.callback_query(AdminCB.filter(F.action == "goals"))
async def goal_breakdown(callback: CallbackQuery, lang: str, services: Services, is_admin: bool = False) -> None:
    if not is_admin:
        await callback.answer("⛔", show_alert=True)
        return
    people, _total = await services.repos.users.paginate(page=1, per_page=2000)
    counts: dict[str, int] = {}
    for person in people:
        key = person.goal or "none"
        counts[key] = counts.get(key, 0) + 1
    ar = lang.startswith("ar")
    lines = [f"<b>🎯 {'توزيع الأهداف' if ar else 'Goal distribution'}</b>"]
    for key, value in sorted(counts.items(), key=lambda kv: -kv[1]):
        lines.append(f"• {get_goal(key).describe(lang)}: {value}")
    await edit_or_send(callback, "\n".join(lines), reply_markup=admin_goal_filter(lang))


__all__ = ["router", "PER_PAGE", "AUDIENCES", "BroadcastStatus", "Any"]
