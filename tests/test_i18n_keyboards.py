"""i18n integrity + keyboard/callback-data smoke tests.

The catalogue scan is the important one: a missing key silently renders as its
own name ("rem.water_msg") in a user's chat, so every ``t(…)`` call site in the
codebase is verified against :data:`app.i18n.STRINGS`.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from aiogram.types import InlineKeyboardMarkup, ReplyKeyboardMarkup, ReplyKeyboardRemove

from app import keyboards as kb
from app.callbacks import (
    AdminCB,
    AdminUserCB,
    CoachCB,
    ConsultCB,
    ForcedCB,
    MenuCB,
    OnboardingCB,
    PlanCB,
    PointsCB,
    PrivacyCB,
    ProgressCB,
    ReferralCB,
    ReminderCB,
    ScanCB,
    SettingsCB,
)
from app.i18n import STRINGS, missing_keys, t
from app.services.goal_registry import all_goals

APP_DIR = Path(__file__).resolve().parents[1] / "app"
T_CALL = re.compile(r"""\bt\(\s*(?:lang|user\.language_code|person\.language_code|[a-z_]+)\s*,\s*["']([\w.]+)["']""")


def _all_source() -> str:
    return "\n".join(path.read_text(encoding="utf-8") for path in APP_DIR.rglob("*.py"))


def test_every_translation_key_used_in_code_exists() -> None:
    used = set(T_CALL.findall(_all_source()))
    assert used, "the scan found no t() calls — the regex is broken"
    unknown = sorted(used - set(STRINGS))
    assert not unknown, f"unknown i18n keys used in app/: {unknown}"


def test_every_key_is_translated_into_both_languages() -> None:
    assert missing_keys("ar") == []
    assert missing_keys("en") == []


def test_t_falls_back_gracefully() -> None:
    assert t("ar", "nope.missing") == "nope.missing"
    assert t(None, "common.cancel")                      # None → Arabic default
    assert t("en", "menu.title", name="Sam") == "<b>Welcome Sam 👋</b>\n\nWhat do you want to work on today?"
    # missing kwargs must not raise — the raw template is returned instead
    assert "{points}" in t("ar", "menu.status_line")


def test_main_menu_labels_match_the_handler_shortcuts() -> None:
    """The reply buttons must match the text sets the routers listen for."""
    from app.handlers import coach, consult, plans, points, progress, scan
    from app.handlers import settings as settings_handler

    markup = kb.main_menu("ar")
    labels = {button.text for row in markup.keyboard for button in row}
    assert labels & consult.MENU_LABELS
    assert labels & coach.MENU_LABELS
    assert labels & scan.MENU_LABELS
    assert labels & progress.MENU_LABELS
    assert labels & plans.MENU_LABELS
    assert labels & points.MENU_LABELS
    assert labels & points.REFERRAL_LABELS
    assert labels & settings_handler.MENU_LABELS

    admin_markup = kb.main_menu("ar", is_admin=True)
    admin_labels = {button.text for row in admin_markup.keyboard for button in row}
    from app.handlers import admin as admin_handler

    assert admin_labels & admin_handler.ADMIN_LABELS


@pytest.mark.parametrize("lang", ["ar", "en"])
def test_every_keyboard_builds_in_both_languages(lang: str, user=None) -> None:  # noqa: ANN001
    builders = {
        "main_menu": lambda: kb.main_menu(lang, is_admin=True, has_coach=True),
        "remove_keyboard": kb.remove_keyboard,
        "language_menu": lambda: kb.language_menu(lang),
        "profile_fields": lambda: kb.profile_fields(lang),
        "privacy_menu": lambda: kb.privacy_menu(lang),
        "confirm_deletion": lambda: kb.confirm_deletion(lang),
        "privacy_consent": lambda: kb.privacy_consent(lang),
        "forced_subscription": lambda: kb.forced_subscription(["@chan"], lang),
        "consult_menu": lambda: kb.consult_menu(lang, ["سؤال؟"]),
        "coach_menu": lambda: kb.coach_menu(lang, unlocked=True, balance=120, today_done=False),
        "scan_menu": lambda: kb.scan_menu(lang, free_left=2, balance=120),
        "progress_menu": lambda: kb.progress_menu(lang),
        "photo_angles": lambda: kb.photo_angles(lang),
        "plan_menu": lambda: kb.plan_menu(lang, has_plan=True, kind="training"),
        "points_menu": lambda: kb.points_menu(lang),
        "ledger_pager": lambda: kb.ledger_pager(lang, page=1, has_more=True),
        "referral_menu": lambda: kb.referral_menu(lang),
        "settings_menu": lambda: kb.settings_menu(lang, tz="Asia/Damascus"),
        "reminders_menu": lambda: kb.reminders_menu(lang, []),
        "admin_menu": lambda: kb.admin_menu(lang),
        "admin_users_pager": lambda: kb.admin_users_pager(lang, page=1, total_pages=3),
        "admin_user_card": lambda: kb.admin_user_card(lang, "00000000-0000-0000-0000-000000000000",
                                                      is_banned=False),
        "admin_broadcast": lambda: kb.admin_broadcast(lang),
        "admin_channels": lambda: kb.admin_channels(lang, enabled=True),
        "admin_config": lambda: kb.admin_config(lang),
        "admin_goal_filter": lambda: kb.admin_goal_filter(lang),
        "onb_gender": lambda: kb.onb_gender(lang),
        "onb_activity": lambda: kb.onb_activity(lang),
        "onb_goals": lambda: kb.onb_goals(lang),
        "onb_equipment": lambda: kb.onb_equipment(lang),
        "onb_budget": lambda: kb.onb_budget(lang),
        "onb_food_style": lambda: kb.onb_food_style(lang),
        "onb_cancel": lambda: kb.onb_cancel(lang),
    }
    for name, build in builders.items():
        markup = build()
        assert isinstance(markup, (InlineKeyboardMarkup, ReplyKeyboardMarkup, ReplyKeyboardRemove)), name
        if isinstance(markup, InlineKeyboardMarkup):
            assert markup.inline_keyboard, f"{name} produced no buttons"


@pytest.mark.parametrize("lang", ["ar", "en"])
def test_label_tables_resolve(lang: str) -> None:
    for table in (kb.GENDER_LABELS, kb.ACTIVITY_LABELS, kb.EQUIPMENT_LABELS, kb.BUDGET_LABELS,
                  kb.FOOD_LABELS, kb.ANGLE_LABELS, kb.REMINDER_LABELS):
        for key in table:
            assert kb._label(table, key, lang)


def test_helper_labels() -> None:
    assert kb.plan_kind_label("boxing", "ar")
    assert kb.subscription_label("monthly", "en") == "Monthly"
    assert kb.is_ar("ar") and not kb.is_ar("en") and kb.is_ar(None)
    assert len(all_goals("ar")) == len(all_goals("en"))


@pytest.mark.parametrize("factory,kwargs", [
    (MenuCB, {"action": "home"}),
    (ConsultCB, {"action": "start"}),
    (CoachCB, {"action": "confirm_unlock"}),
    (ScanCB, {"action": "manual"}),
    (ProgressCB, {"action": "angle", "arg": "front"}),
    (PlanCB, {"action": "generate", "kind": "boxing"}),
    (PointsCB, {"action": "ledger", "page": 2}),
    (ReferralCB, {"action": "share"}),
    (ReminderCB, {"kind": "water", "action": "toggle"}),
    (SettingsCB, {"action": "field", "arg": "weight"}),
    (PrivacyCB, {"action": "confirm_delete"}),
    (ForcedCB, {"action": "check"}),
    (OnboardingCB, {"step": "goal", "value": "cut"}),
    (AdminCB, {"action": "users", "page": 3}),
    (AdminUserCB, {"action": "profile", "user_id": "00000000-0000-0000-0000-000000000000"}),
])
def test_callback_data_round_trips(factory, kwargs: dict) -> None:  # noqa: ANN001
    packed = factory(**kwargs).pack()
    assert isinstance(packed, str)
    parsed = factory.unpack(packed)
    for key, value in kwargs.items():
        assert getattr(parsed, key) == value


def test_callback_prefixes_are_unique() -> None:
    prefixes = [factory.__prefix__ for factory in (
        MenuCB, ConsultCB, CoachCB, ScanCB, ProgressCB, PlanCB, PointsCB, ReferralCB,
        ReminderCB, SettingsCB, PrivacyCB, ForcedCB, OnboardingCB, AdminCB, AdminUserCB,
    )]
    assert len(prefixes) == len(set(prefixes)), "two callback factories share a prefix"


def test_callback_payload_fits_telegrams_64_byte_limit() -> None:
    """Telegram rejects callback_data longer than 64 bytes."""
    worst = AdminUserCB(action="profile", user_id="00000000-0000-0000-0000-000000000000",
                        arg="x" * 12, page=99).pack()
    assert len(worst.encode()) <= 64, f"{worst!r} is {len(worst.encode())} bytes"
