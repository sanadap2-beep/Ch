"""/start, help, language, privacy consent and the forced-subscription gate."""
from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.config import settings
from app.db.models import User
from app.handlers.callbacks import ForcedCB, MenuCB, PrivacyCB
from app.i18n import t
from app.keyboards import (
    forced_subscription,
    main_menu,
    privacy_consent,
    remove_keyboard,
)
from app.services.context import Services
from app.services.goal_registry import get_goal
from app.services.privacy import POLICY_VERSION
from app.services.referral import parse_start_param
from app.utils.text import escape_html
from app.utils.tg import answer_long, edit_or_send, send_long

logger = logging.getLogger(__name__)

router = Router(name="start")

MENU_TEXTS = {"🏠 القائمة الرئيسية", "🏠 Main menu"}


def is_admin(tg_id: int) -> bool:
    return tg_id in settings.admin_ids


# ═══════════════════════════════════════════════════════════════════════════
#  /start
# ═══════════════════════════════════════════════════════════════════════════
@router.message(CommandStart(deep_link=True))
async def cmd_start(
    message: Message,
    command: CommandObject,
    user: User,
    lang: str,
    services: Services,
    state: FSMContext,
    is_new_user: bool = False,
) -> None:
    await state.clear()

    # referral attribution from a ?start=ref_CODE deep link
    code = parse_start_param(command.args if command else None)
    if code:
        outcome = await services.referral.attach_inviter(user, code)
        if outcome.invitee_points and outcome.inviter:
            await message.answer(
                t(lang, "ref.welcome_referred",
                  name=escape_html(outcome.inviter.full_name), points=outcome.invitee_points)
            )
    elif is_new_user:
        await services.points.welcome_bonus(user)

    if services.privacy.needs_consent(user):
        await send_long(
            message.bot,
            message.chat.id,
            f"<b>{t(lang, 'privacy.title')}</b>\n\n{services.privacy.policy_text(lang)}\n\n{t(lang, 'privacy.ask')}",
            reply_markup=privacy_consent(lang),
        )
        return

    if not user.onboarding_done:
        from app.handlers.onboarding import begin_onboarding

        await begin_onboarding(message, user, lang, services, state)
        return

    await show_menu_message(message, user, lang, services)


@router.message(Command("help"))
async def cmd_help(message: Message, lang: str) -> None:
    await send_long(message.bot, message.chat.id, help_text(lang), reply_markup=main_menu(lang))


@router.message(Command("menu"))
@router.message(F.text.in_(MENU_TEXTS))
async def cmd_menu(message: Message, user: User, lang: str, services: Services) -> None:
    if not user.onboarding_done:
        await message.answer(t(lang, "onb.welcome"), reply_markup=remove_keyboard())
        return
    await show_menu_message(message, user, lang, services)


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, lang: str, state: FSMContext) -> None:
    await state.clear()
    await message.answer(t(lang, "common.cancel"), reply_markup=remove_keyboard())


@router.message(Command("lang"))
async def cmd_lang(message: Message, user: User, services: Services) -> None:
    new_lang = "en" if (user.language_code or "ar").startswith("ar") else "ar"
    user.language_code = new_lang
    await services.repos.session.flush()
    await message.answer(
        t(new_lang, "lang.changed"),
        reply_markup=main_menu(new_lang, is_admin=is_admin(message.from_user.id)),
    )


@router.message(Command("id"))
async def cmd_id(message: Message, lang: str) -> None:
    """Users need their numeric id for manual top-ups."""
    ar = lang.startswith("ar")
    await message.answer(
        ("معرّفك (أرسله للإدارة لشحن النقاط): " if ar else "Your ID (send it to the admin for top-ups): ")
        + f"<code>{message.from_user.id}</code>"
    )


# ═══════════════════════════════════════════════════════════════════════════
#  privacy consent
# ═══════════════════════════════════════════════════════════════════════════
@router.callback_query(PrivacyCB.filter(F.action == "accept"))
async def privacy_accept(
    callback: CallbackQuery, user: User, lang: str, services: Services, state: FSMContext
) -> None:
    await services.privacy.record_consent(user, granted=True)
    text = t(lang, "privacy.accepted", version=POLICY_VERSION)
    if not user.onboarding_done:
        from app.handlers.onboarding import begin_onboarding

        await callback.answer(text)
        await begin_onboarding(callback.message, user, lang, services, state)
        return
    await edit_or_send(callback, text, reply_markup=main_menu(lang, is_admin=is_admin(callback.from_user.id)))


@router.callback_query(PrivacyCB.filter(F.action == "decline"))
async def privacy_decline(callback: CallbackQuery, user: User, lang: str, services: Services) -> None:
    await services.privacy.record_consent(user, granted=False)
    await callback.answer(t(lang, "privacy.declined"), show_alert=True)


@router.callback_query(PrivacyCB.filter(F.action == "policy"))
async def privacy_policy(callback: CallbackQuery, lang: str, services: Services) -> None:
    await answer_long(callback, f"<b>{t(lang, 'privacy.title')}</b>\n\n{services.privacy.policy_text(lang)}")


# ═══════════════════════════════════════════════════════════════════════════
#  forced channel subscription
# ═══════════════════════════════════════════════════════════════════════════
@router.callback_query(ForcedCB.filter(F.action == "check"))
async def forced_check(callback: CallbackQuery, user: User, lang: str, services: Services) -> None:
    from app.middlewares.auth import _check_channels, _forced_channels

    channels = await _forced_channels(services)
    if not channels or await _check_channels(callback.bot, callback.from_user.id, channels):
        await callback.answer(t(lang, "forced.ok"))
        await show_menu_callback(callback, user, lang, services)
        return
    await edit_or_send(
        callback,
        t(lang, "forced.body", channels="\n".join(f"• {c}" for c in channels))
        + f"\n\n{t(lang, 'forced.fail')}",
        reply_markup=forced_subscription(channels, lang),
    )


# ═══════════════════════════════════════════════════════════════════════════
#  shared menu rendering
# ═══════════════════════════════════════════════════════════════════════════
async def menu_text(user: User, lang: str, services: Services) -> str:
    log = await services.repos.tracking.daily_log(user, user.local_today())
    lines = [
        t(lang, "menu.title", name=escape_html(user.full_name)),
        t(lang, "menu.status_line",
          points=int(user.points_balance or 0),
          streak=int(user.streak_current or 0),
          goal=get_goal(user.goal).describe(lang)),
        t(lang, "menu.today_line",
          eaten=int(log.consumed_calories or 0),
          target=int(log.target_calories or 0),
          left=int(log.remaining_calories),
          water=int(log.water_ml or 0),
          water_target=int(log.water_target_ml or 0)),
    ]
    if not user.has_coach_access:
        lines.append(t(lang, "coach.locked", cost=settings.coach_access_cost_points,
                       balance=int(user.points_balance or 0)))
    return "\n".join(lines)


async def show_menu_message(message: Message, user: User, lang: str, services: Services) -> None:
    await send_long(
        message.bot, message.chat.id, await menu_text(user, lang, services),
        reply_markup=main_menu(lang, is_admin=is_admin(message.from_user.id),
                               has_coach=user.has_coach_access),
    )


async def show_menu_callback(callback: CallbackQuery, user: User, lang: str, services: Services) -> None:
    await edit_or_send(
        callback, await menu_text(user, lang, services),
        reply_markup=main_menu(lang, is_admin=is_admin(callback.from_user.id),
                               has_coach=user.has_coach_access),
    )


@router.callback_query(MenuCB.filter(F.action == "home"))
async def cb_home(callback: CallbackQuery, user: User, lang: str, services: Services) -> None:
    await show_menu_callback(callback, user, lang, services)


def help_text(lang: str) -> str:
    ar = lang.startswith("ar")
    if ar:
        return (
            "<b>🏋️ الكوتش الذكي — دليل سريع</b>\n\n"
            "• <b>💬 الاستشارة العامة</b> — مجانية، أي سؤال رياضي/تغذوي مع روابط فيديو للتمارين.\n"
            f"• <b>🏆 الكوتش الشخصي 24 ساعة</b> — يحتاج {settings.coach_access_cost_points} نقطة: متابعة يومية، "
            "تعديل سعرات تلقائي حسب تقدمك، خصم صور أكلك من يومك، وتقارير أسبوعية وشهرية.\n"
            "• <b>🍽️ حساب سعرات الأكل</b> — أرسل صورة وجبتك وأحللها، وأسألك عند الغموض بدل التخمين.\n"
            "• <b>📈 تقدمي</b> — الوزن، القياسات، صور التقدم، الرسم البياني، وتصدير رحلتك PDF.\n"
            "• <b>📋 برنامجي</b> — توليد برنامج تمارين/وجبات/ملاكمة حسب معداتك وميزانيتك وإصاباتك.\n"
            "• <b>🪙 نقاطي</b> — الرصيد والسجل، ودعوة صديق = نقاط، والاشتراك الشهري.\n\n"
            "<b>اختصارات:</b> اكتب «وزني 81.4» أو «شربت كاستين ماء» أو أرسل صورة أكلك مباشرة.\n\n"
            "⚠️ البوت ليس بديلًا عن الطبيب. عند وجود إصابة أو حالة صحية راجع مختصًا."
        )
    return (
        "<b>🏋️ AI Coach — quick guide</b>\n\n"
        "• <b>💬 General consultation</b> — free, any fitness/nutrition question with exercise video links.\n"
        f"• <b>🏆 24h personal coach</b> — {settings.coach_access_cost_points} points: daily tracking, automatic "
        "calorie adjustment, food photos deducted from your day, weekly & monthly reports.\n"
        "• <b>🍽️ Food calorie scan</b> — send a photo; I analyse it and ask when something is ambiguous.\n"
        "• <b>📈 Progress</b> — weight, measurements, photos, charts, PDF export.\n"
        "• <b>📋 Programme</b> — training/meals/boxing plans around your equipment, budget and injuries.\n"
        "• <b>🪙 My points</b> — balance & ledger, invite a friend for points, monthly subscription.\n\n"
        "<b>Shortcuts:</b> type “weight 81.4”, “drank 2 glasses of water”, or send a food photo.\n\n"
        "⚠️ Not a replacement for a doctor. With an injury or condition, see a specialist."
    )


__all__ = ["router", "help_text", "menu_text", "show_menu_message", "show_menu_callback", "is_admin"]
