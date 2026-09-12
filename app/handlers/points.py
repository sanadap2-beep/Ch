"""Points panel, ledger, referral programme and subscription status."""
from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.types import CallbackQuery, Message

from app.config import settings
from app.constants import SubscriptionPlan
from app.db.models import User
from app.handlers.callbacks import MenuCB, PointsCB, ReferralCB
from app.i18n import t
from app.keyboards import (
    ledger_pager,
    points_menu,
    referral_menu,
    subscription_label,
)
from app.services.context import Services
from app.services.streak import badge_label
from app.utils.text import escape_html
from app.utils.tg import edit_or_send, send_long

logger = logging.getLogger(__name__)

router = Router(name="points")

MENU_LABELS = {"🪙 نقاطي", "🪙 My points"}
REFERRAL_LABELS = {"🎁 دعوة صديق", "🎁 Invite a friend"}
PAGE_SIZE = 8


def _contact(lang: str) -> str:
    handle = settings.support_username
    if not handle:
        return ""
    if not handle.startswith("@"):
        handle = f"@{handle}"
    return f": <b>{escape_html(handle)}</b>"


def _prices(lang: str) -> str:
    ar = lang.startswith("ar")
    rows = [
        (("💬 استشارة عامة" if ar else "💬 General consult"),
         (f"{settings.consult_free_daily_limit} مجانية/يوم" if ar else f"{settings.consult_free_daily_limit} free/day")),
        (("🏆 كوتش شخصي 24 ساعة" if ar else "🏆 24h personal coach"),
         f"{settings.coach_access_cost_points}"),
        (("💪 رسالة للكوتش" if ar else "💪 Coach message"),
         (f"{settings.coach_points_minimum}–{settings.coach_points_maximum} (حسب الطول)" if ar
          else f"{settings.coach_points_minimum}–{settings.coach_points_maximum} (token-metered)")),
        (("🍽️ صورة أكل" if ar else "🍽️ Food photo"),
         (f"{settings.food_scan_free_daily} مجانية ثم {settings.food_scan_cost_points}" if ar
          else f"{settings.food_scan_free_daily} free then {settings.food_scan_cost_points}")),
        (("📸 صورة تقدم الجسم" if ar else "📸 Progress photo"), f"{settings.progress_scan_cost_points}"),
    ]
    if settings.report_export_cost_points:
        rows.append((("📄 تصدير PDF" if ar else "📄 PDF export"), f"{settings.report_export_cost_points}"))
    return "\n".join(t(lang, "points.prices_line", label=label, cost=cost) for label, cost in rows)


# ═══════════════════════════════════════════════════════════════════════════
#  balance panel
# ═══════════════════════════════════════════════════════════════════════════
async def panel(event: Message | CallbackQuery, user: User, lang: str, services: Services) -> None:
    await services.repos.economy.ensure_windows(user)
    ar = lang.startswith("ar")
    text = t(
        lang, "points.title",
        balance=int(user.points_balance or 0),
        earned=int(user.total_points_earned or 0),
        spent=int(user.total_points_spent or 0),
        prices=_prices(lang),
    )
    text += "\n\n" + (
        f"📈 {'استخدام اليوم' if ar else 'Today'}: {int(user.daily_points_spent or 0)}/"
        f"{settings.user_daily_points_cap} | "
        f"{'هذا الشهر' if ar else 'Month'}: {int(user.monthly_points_spent or 0)}/"
        f"{settings.user_monthly_points_cap}"
    )
    text += "\n\n" + services.subscription.status_text(user, lang)
    markup = points_menu(lang)
    if isinstance(event, CallbackQuery):
        await edit_or_send(event, text, reply_markup=markup)
    else:
        await send_long(event.bot, event.chat.id, text, reply_markup=markup)


@router.message(F.text.in_(MENU_LABELS))
async def menu_button(message: Message, user: User, lang: str, services: Services) -> None:
    await panel(message, user, lang, services)


@router.callback_query(MenuCB.filter(F.action == "points"))
async def menu_callback(callback: CallbackQuery, user: User, lang: str, services: Services) -> None:
    await panel(callback, user, lang, services)


@router.callback_query(PointsCB.filter(F.action == "balance"))
async def balance(callback: CallbackQuery, user: User, lang: str, services: Services) -> None:
    await panel(callback, user, lang, services)


# ═══════════════════════════════════════════════════════════════════════════
#  ledger (paginated)
# ═══════════════════════════════════════════════════════════════════════════
@router.callback_query(PointsCB.filter(F.action == "ledger"))
async def ledger(callback: CallbackQuery, cb: PointsCB, user: User, lang: str, services: Services) -> None:
    ar = lang.startswith("ar")
    page = max(0, cb.page)
    entries = await services.repos.economy.ledger(user.id, limit=PAGE_SIZE, offset=page * PAGE_SIZE)
    total = await services.repos.economy.ledger_count(user.id)
    if not entries:
        await edit_or_send(
            callback, ("لا توجد حركات بعد." if ar else "No entries yet."), reply_markup=points_menu(lang, page)
        )
        return
    lines = [t(lang, "points.ledger_title", n=total)]
    for entry in entries:
        sign = "+" if entry.amount > 0 else "−"
        stamp = entry.created_at.strftime("%m-%d %H:%M") if entry.created_at else ""
        reason = escape_html(str(entry.reason or ""))
        note = escape_html(str(entry.note or ""))
        lines.append(f"{stamp} {sign}<b>{abs(int(entry.amount))}</b> · {reason}" + (f" — {note}" if note else ""))
    has_more = (page + 1) * PAGE_SIZE < total
    await edit_or_send(callback, "\n".join(lines),
                       reply_markup=ledger_pager(lang, page=page, has_more=has_more))


# ═══════════════════════════════════════════════════════════════════════════
#  how to get points (admin top-up is the only paid path for now)
# ═══════════════════════════════════════════════════════════════════════════
@router.callback_query(PointsCB.filter(F.action == "get"))
async def how_to_get(callback: CallbackQuery, user: User, lang: str, services: Services) -> None:
    ar = lang.startswith("ar")
    lines = [
        f"<b>{'➕ كيف تحصل على نقاط؟' if ar else '➕ How do I get points?'}</b>",
        f"1️⃣ {t(lang, 'points.contact_admin', contact=_contact(lang), tg_id=user.tg_id)}",
        f"2️⃣ {'ادعُ صديقًا' if ar else 'Invite a friend'}: "
        f"+{settings.referral_inviter_points} "
        f"{'لك و' if ar else 'for you and '} +{settings.referral_invitee_points} "
        f"{'له عن كل تسجيل مكتمل' if ar else 'for them per completed sign-up'}"
        + (f" — {'ومكافأة' if ar else 'plus a bonus'} +{settings.referral_bonus_points} "
           f"{'كل' if ar else 'every'} {settings.referral_bonus_every}"
           if settings.referral_bonus_every else ""),
        f"3️⃣ {'الاشتراك الشهري' if ar else 'Monthly subscription'}: "
        f"+{settings.monthly_plan_points} {'نقطة كل 30 يوم' if ar else 'points every 30 days'} "
        f"({'يُفعّله الأدمن' if ar else 'activated by the admin'})",
        f"4️⃣ {'جوائز الالتزام' if ar else 'Streak rewards'}: +{settings.streak_reward_points} "
        f"{'نقطة عند كل إنجاز' if ar else 'points per milestone'} "
        f"({', '.join(str(m) for m in settings.streak_milestones)} "
        f"{'يوم' if ar else 'days'})",
        "",
        t(lang, "points.get_more"),
    ]
    await edit_or_send(callback, "\n".join(lines), reply_markup=points_menu(lang))


# ═══════════════════════════════════════════════════════════════════════════
#  referral programme (spec extra §6)
# ═══════════════════════════════════════════════════════════════════════════
async def referral_text(user: User, lang: str, services: Services) -> str:
    stats = await services.referral.stats(user)
    return t(
        lang, "ref.title",
        link=escape_html(stats.get("link") or services.referral.link_for(user)),
        code=escape_html(user.referral_code),
        inviter=settings.referral_inviter_points,
        invitee=settings.referral_invitee_points,
        every=settings.referral_bonus_every or "—",
        bonus=settings.referral_bonus_points,
        count=stats.get("count", 0),
    )


@router.message(F.text.in_(REFERRAL_LABELS))
async def referral_button(message: Message, user: User, lang: str, services: Services) -> None:
    await send_long(message.bot, message.chat.id, await referral_text(user, lang, services),
                    reply_markup=referral_menu(lang))


@router.callback_query(MenuCB.filter(F.action == "referral"))
@router.callback_query(ReferralCB.filter(F.action.in_({"card", "stats"})))
async def referral_card(callback: CallbackQuery, user: User, lang: str, services: Services) -> None:
    await edit_or_send(callback, await referral_text(user, lang, services), reply_markup=referral_menu(lang))


@router.callback_query(ReferralCB.filter(F.action == "link"))
async def referral_link(callback: CallbackQuery, user: User, lang: str, services: Services) -> None:
    link = services.referral.link_for(user)
    await callback.answer(f"{link}?start=ref_{user.referral_code}", show_alert=True)


@router.callback_query(ReferralCB.filter(F.action == "share"))
async def referral_share(callback: CallbackQuery, user: User, lang: str, services: Services) -> None:
    """Show the ready-to-forward invitation text."""
    await callback.answer()
    await send_long(callback.bot, callback.message.chat.id,
                    services.referral.share_text(user, lang), reply_markup=referral_menu(lang))


# ═══════════════════════════════════════════════════════════════════════════
#  subscription status (spec extra §10 — admin-activated, auto-renewing)
# ═══════════════════════════════════════════════════════════════════════════
@router.callback_query(PointsCB.filter(F.action == "subscription"))
async def subscription_panel(callback: CallbackQuery, user: User, lang: str, services: Services) -> None:
    ar = lang.startswith("ar")
    status = services.subscription.status_text(user, lang)
    plans = " · ".join(
        f"{subscription_label(plan.value, lang)}: +{points}"
        for plan, points in (
            (SubscriptionPlan.MONTHLY, settings.monthly_plan_points),
            (SubscriptionPlan.QUARTERLY, settings.quarterly_plan_points),
        )
    )
    text = (
        f"{status}\n\n<b>{'الباقات' if ar else 'Plans'}</b>\n{plans}\n\n"
        + t(lang, "points.contact_admin", contact=_contact(lang), tg_id=user.tg_id)
    )
    await edit_or_send(callback, text, reply_markup=points_menu(lang))


# ═══════════════════════════════════════════════════════════════════════════
#  badges (spec extra §6)
# ═══════════════════════════════════════════════════════════════════════════
@router.callback_query(PointsCB.filter(F.action == "badges"))
async def badges(callback: CallbackQuery, user: User, lang: str, services: Services) -> None:
    ar = lang.startswith("ar")
    owned = await services.repos.economy.badges_of(user.id)
    lines = [f"<b>🏅 {'أوسمتك' if ar else 'Your badges'}</b>"]
    if owned:
        for badge in owned:
            stamp = badge.earned_at.strftime("%Y-%m-%d") if badge.earned_at else ""
            lines.append(f"• {badge_label(badge.badge_key, lang)} — {stamp}")
    else:
        lines.append(
            "ما حصلت على أوسمة بعد — ابدأ بيومين متتاليين 💪" if ar else "No badges yet — start with two days in a row 💪"
        )
    lines.append(f"\n🔥 {'سلسلتك الحالية' if ar else 'Current streak'}: {int(user.streak_current or 0)} | "
                 f"{'أفضل سلسلة' if ar else 'best'}: {int(user.streak_best or 0)}")
    await edit_or_send(callback, "\n".join(lines), reply_markup=points_menu(lang))
