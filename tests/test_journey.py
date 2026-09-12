"""The whole product in one run: a new user's journey from /start to deleting their data.

Every step goes through the production dispatcher with real ``Update`` objects, so
this is the test that answers "does the bot actually work end to end?" rather than
"does this service work in isolation?".
"""
from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

import pytest

from app.callbacks import (
    CoachCB,
    OnboardingCB,
    PlanCB,
    PointsCB,
    PrivacyCB,
    ProgressCB,
    ReferralCB,
    ReminderCB,
    SettingsCB,
)
from app.config import settings
from app.db.models import User, as_aware
from tests import conftest
from tests.telegram_mock import feed, photo_update
from tests.test_handlers import (
    ADMIN_TG_ID,
    NEW_TG_ID,
    USER_TG_ID,
    _fresh_user,
    _ledger,
    _meals,
    _measurements,
    _plans,
    _reminders,
    _today_log,
    _weights,
    say,
    tap,
)

INVITER_TG_ID = 8080001


@pytest.fixture
def journey_user(session) -> User:
    """The inviter: an existing, onboarded user with a referral code."""
    person = conftest.make_user(tg_id=INVITER_TG_ID, referral_code="JOURNEY1",
                                username="inviter", display_name="Inviter")
    person.onboarding_completed_at = dt.datetime.now(dt.UTC)
    person.privacy_consent_at = dt.datetime.now(dt.UTC)
    person.privacy_policy_version = "1.0"
    person.target_calories = 2200
    person.points_balance = 300
    session.add(person)
    return person


def _relax_throttle(tg: SimpleNamespace) -> None:
    """The journey sends dozens of updates with no gap between them.

    A real user answers onboarding over ~30 seconds, well inside the 8-per-6-seconds
    limit; the simulated one would trip it constantly. ``test_throttle_limits_a_burst``
    still asserts the guard works with its default settings.
    """
    from app.middlewares.auth import ThrottlingMiddleware

    for observer in (tg.dp.message, tg.dp.callback_query, tg.dp.my_chat_member,
                     tg.dp.edited_message, tg.dp.inline_query):
        for middleware in list(observer.outer_middleware) + list(observer.middleware):
            if isinstance(middleware, ThrottlingMiddleware):
                middleware.limit = 100_000


async def test_full_user_journey(tg: SimpleNamespace, session, journey_user: User) -> None:
    await session.commit()
    _relax_throttle(tg)
    newbie = NEW_TG_ID + 5

    # ── stage 1: arrival through a referral deep link ────────────────────────
    api = await say(tg, "/start ref_JOURNEY1", tg_id=newbie)
    assert "سياسة الخصوصية" in api.all_text()

    invitee = await _fresh_user(tg, newbie)
    inviter = await _fresh_user(tg, INVITER_TG_ID)
    assert invitee.referred_by_id == inviter.id
    assert invitee.points_balance == settings.referral_invitee_points

    # ── stage 2: consent, then the whole sign-up flow ────────────────────────
    await tap(tg, PrivacyCB(action="accept").pack(), tg_id=newbie)
    await say(tg, "ليان", tg_id=newbie)
    await say(tg, "٣٠", tg_id=newbie)                       # Arabic-Indic digits
    await tap(tg, OnboardingCB(step="gender", value="female").pack(), tg_id=newbie)
    await say(tg, "165", tg_id=newbie)
    await say(tg, "92", tg_id=newbie)
    await say(tg, "68", tg_id=newbie)
    await tap(tg, OnboardingCB(step="activity", value="moderate").pack(), tg_id=newbie)
    await tap(tg, OnboardingCB(step="goal", value="cut").pack(), tg_id=newbie)
    await tap(tg, OnboardingCB(step="equipment", value="gym").pack(), tg_id=newbie)
    await say(tg, "عندي ألم في الركبة", tg_id=newbie)        # injury → safety screen
    await tap(tg, OnboardingCB(step="budget", value="low").pack(), tg_id=newbie)
    await tap(tg, OnboardingCB(step="food", value="local_popular").pack(), tg_id=newbie)
    api = await say(tg, "لا شيء", tg_id=newbie)

    person = await _fresh_user(tg, newbie)
    assert person.onboarding_completed_at is not None
    assert person.display_name == "ليان" and person.age == 30 and person.gender == "female"
    assert person.height_cm == 165 and person.weight_kg == 92 and person.target_weight_kg == 68
    assert person.bmr and person.tdee
    assert 1200 < person.target_calories < person.tdee       # a deficit, but not a crash diet
    assert person.target_protein_g > 0
    assert {item.get("area") for item in person.injuries} == {"knee"}
    assert person.preferences.get("restricted_movements")    # knee-unsafe moves excluded
    assert person.preferences.get("safe_alternatives")
    assert api.all_text()                                    # summary + main menu

    # the inviter is paid only now that the sign-up is real
    paid = await _fresh_user(tg, INVITER_TG_ID)
    assert paid.points_balance > 300

    # default reminders were seeded and armed for a brand-new user
    rules = await _reminders(tg, newbie)
    assert rules, "no default reminders were created"
    assert any(rule.enabled and rule.next_run_at is not None for rule in rules)

    # ── stage 3: free consultation (product A) ───────────────────────────────
    api = await say(tg, "💬 الاستشارة العامة", tg_id=newbie)
    assert api.by_name("sendMessage")
    api = await say(tg, "أعطني تمارين تحرق 300 سعرة", tg_id=newbie)
    assert "بيربي" in api.all_text() or "خطة سريعة" in api.all_text()
    assert (await _fresh_user(tg, newbie)).daily_consult_count >= 1

    # ── stage 4: unlock the 24h coach (product B) ────────────────────────────
    api = await say(tg, "🏆 الكوتش الشخصي 24س", tg_id=newbie)
    assert "🔒" in api.all_text()

    balance_before = (await _fresh_user(tg, newbie)).points_balance
    if balance_before < settings.coach_access_cost_points:
        # top the user up the way an admin would, so the journey can continue
        await _grant(tg, session, newbie, settings.coach_access_cost_points)

    api = await tap(tg, CoachCB(action="confirm_unlock").pack(), tg_id=newbie)
    person = await _fresh_user(tg, newbie)
    assert person.has_coach_access is True
    assert as_aware(person.coach_access_until) > dt.datetime.now(dt.UTC)
    ledger = await _ledger(tg, newbie)
    assert any(e.amount == -settings.coach_access_cost_points for e in ledger)

    # ── stage 5: daily tracking through natural language ─────────────────────
    await say(tg, "وزني اليوم 90.8", tg_id=newbie)
    person = await _fresh_user(tg, newbie)
    assert person.weight_kg == pytest.approx(90.8)
    assert any(abs(r.weight_kg - 90.8) < 0.01 for r in await _weights(tg, newbie))

    await say(tg, "شربت كاستين ماء", tg_id=newbie)
    log = await _today_log(tg, newbie)
    assert log.water_ml >= 500

    api = await tap(tg, CoachCB(action="workout_done").pack(), tg_id=newbie)
    assert (await _today_log(tg, newbie)).workout_done is True

    # ── stage 6: food photo deduction (product C) ────────────────────────────
    await say(tg, "🍽️ حساب سعرات الأكل", tg_id=newbie)
    tg.api.clear()
    await feed(tg.dp, tg.bot, photo_update(tg_id=newbie, caption="غدائي"))
    assert "شاورما" in tg.api.all_text() or "800" in tg.api.all_text()

    log = await _today_log(tg, newbie)
    assert log.consumed_calories >= 800
    assert log.remaining_calories < person.target_calories    # deducted from the day
    meals = await _meals(tg, newbie)
    assert meals and meals[0].calories == 800

    # an unclear photo must ask, never guess
    ambiguous = dict(conftest.CANNED["food_analysis"])
    ambiguous["clarifications"] = ["كم كمية الرز؟"]
    ambiguous["confidence"] = 0.25
    original = conftest.CANNED["food_analysis"]
    conftest.CANNED["food_analysis"] = ambiguous
    try:
        tg.api.clear()
        await feed(tg.dp, tg.bot, photo_update(tg_id=newbie))
        assert "كم كمية الرز" in tg.api.all_text()
        eaten_before = (await _today_log(tg, newbie)).consumed_calories
        await say(tg, "كوب ونص", tg_id=newbie)
        assert (await _today_log(tg, newbie)).consumed_calories >= eaten_before
    finally:
        conftest.CANNED["food_analysis"] = original

    # ── stage 7: a knee-safe training programme ──────────────────────────────
    api = await say(tg, "📋 برنامجي", tg_id=newbie)
    assert api.by_name("sendMessage")
    api = await tap(tg, PlanCB(action="generate", kind="training").pack(), tg_id=newbie)
    assert api.all_text()
    plans = await _plans(tg, newbie)
    assert any(p.kind == "training" for p in plans)

    api = await tap(tg, PlanCB(action="today").pack(), tg_id=newbie)
    assert api.all_text()

    # ── stage 8: progress, charts, reports, PDF export ───────────────────────
    await tap(tg, ProgressCB(action="measurements").pack(), tg_id=newbie)
    await say(tg, "الخصر 88", tg_id=newbie)
    rows = await _measurements(tg, newbie)
    assert rows and rows[0].waist_cm == pytest.approx(88)

    api = await tap(tg, ProgressCB(action="chart").pack(), tg_id=newbie)
    assert api.by_name("sendPhoto"), "the progress chart was never sent"

    api = await tap(tg, ProgressCB(action="report_week").pack(), tg_id=newbie)
    assert api.all_text()

    api = await tap(tg, ProgressCB(action="export").pack(), tg_id=newbie)
    assert api.by_name("sendDocument"), "the PDF journey export was never sent"

    # ── stage 9: points, referral and subscription panels ────────────────────
    api = await say(tg, "🪙 نقاطي", tg_id=newbie)
    assert api.all_text()
    api = await tap(tg, PointsCB(action="ledger", page=0).pack(), tg_id=newbie)
    assert api.all_text()
    api = await say(tg, "🎁 دعوة صديق", tg_id=newbie)
    person = await _fresh_user(tg, newbie)
    assert person.referral_code in api.all_text()             # the user's own code
    api = await tap(tg, ReferralCB(action="link").pack(), tg_id=newbie)
    assert "t.me" in api.all_text() or api.alerts()
    api = await tap(tg, PointsCB(action="subscription").pack(), tg_id=newbie)
    assert api.all_text()

    # ── stage 10: switch to English and keep going ───────────────────────────
    await say(tg, "⚙️ الإعدادات", tg_id=newbie)
    await tap(tg, SettingsCB(action="language").pack(), tg_id=newbie)
    await tap(tg, SettingsCB(action="lang", arg="en").pack(), tg_id=newbie)
    assert (await _fresh_user(tg, newbie)).language_code == "en"

    api = await say(tg, "🪙 My points", tg_id=newbie)
    assert "points" in api.all_text().lower()
    # the Telegram client locale must not undo the explicit choice
    assert (await _fresh_user(tg, newbie)).language_code == "en"

    # ── stage 11: reminders ──────────────────────────────────────────────────
    await say(tg, "⚙️ Settings", tg_id=newbie)
    api = await tap(tg, SettingsCB(action="reminders").pack(), tg_id=newbie)
    assert api.all_text()
    await tap(tg, ReminderCB(kind="water", action="toggle").pack(), tg_id=newbie)
    water = [r for r in await _reminders(tg, newbie) if r.kind == "water"]
    assert water and water[0].enabled is False

    # ── stage 12: privacy — export, then delete everything ───────────────────
    api = await tap(tg, PrivacyCB(action="export").pack(), tg_id=newbie)
    assert api.by_name("sendDocument")

    api = await tap(tg, PrivacyCB(action="delete_photos").pack(), tg_id=newbie)
    assert api.all_text()

    api = await tap(tg, PrivacyCB(action="delete_account").pack(), tg_id=newbie)
    assert api.all_text()
    api = await tap(tg, PrivacyCB(action="confirm_delete").pack(), tg_id=newbie)

    person = await _fresh_user(tg, newbie)
    assert person.data_deleted_at is not None
    assert person.weight_kg is None and person.display_name is None
    assert person.points_balance == 0
    assert person.is_banned is True                            # the shell cannot be reused
    assert await _meals(tg, newbie) == []
    assert await _weights(tg, newbie) == []
    assert await _measurements(tg, newbie) == []


async def _grant(tg: SimpleNamespace, session, tg_id: int, points: int) -> None:
    """Admin-style top-up so the journey can continue when the balance is short."""
    from app.db.base import session_scope
    from app.services.context import Services

    async with session_scope() as other:
        services = Services(other, tg.bot)
        try:
            person = await services.repos.users.by_tg_id(tg_id)
            assert person is not None
            await services.points.admin_grant(person, points, admin_tg_id=ADMIN_TG_ID,
                                              note="journey top-up")
        finally:
            await services.close()


async def test_journey_survives_an_ai_outage(tg: SimpleNamespace, user: User, monkeypatch) -> None:
    """With no working model the bot must degrade politely and never charge."""
    from app.ai.client import NanoGPTClient
    from app.ai.errors import AIAllModelsFailedError

    async def outage(self, *args, **kwargs):
        raise AIAllModelsFailedError("every model failed", task="coach")

    # replaces conftest's offline AI stand-in with a hard failure at the transport
    monkeypatch.setattr(NanoGPTClient, "complete", outage)
    _relax_throttle(tg)
    balance_before = (await _fresh_user(tg, USER_TG_ID)).points_balance

    api = await say(tg, "وزني 85")
    assert api.by_name("sendMessage"), "the user got nothing back during an AI outage"

    person = await _fresh_user(tg, USER_TG_ID)
    assert person.weight_kg == pytest.approx(85.0)           # local parsing still worked
    assert person.points_balance >= balance_before           # no charge for a failed call
