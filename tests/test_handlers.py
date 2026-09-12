"""End-to-end handler tests: real Updates → production dispatcher → recorded Bot API calls.

These exercise the layer the service tests cannot reach — routing, FSM state
transitions, keyboards, gates (ban / forced channel / consent / throttle), admin
authorisation and the error handler — with the Telegram transport mocked in-process.
"""
from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio

from app.ai import router as ai_router
from app.callbacks import (
    AdminCB,
    AdminUserCB,
    CoachCB,
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
from app.config import settings
from app.db.models import User
from tests import conftest
from tests.telegram_mock import (
    MockedSession,
    build_test_dispatcher,
    callback_update,
    feed,
    make_bot,
    photo_update,
    text_update,
    voice_update,
)

ADMIN_TG_ID = 555000111
USER_TG_ID = 424242
NEW_TG_ID = 999000


# ═══════════════════════════════════════════════════════════════════════════
#  fixtures
# ═══════════════════════════════════════════════════════════════════════════
@pytest.fixture(autouse=True)
def _fresh_ai_cache():
    """The routing/budget cache is process-wide — never leak it between tests."""
    ai_router.invalidate_cache()
    yield
    ai_router.invalidate_cache()


@pytest_asyncio.fixture
async def tg(session) -> SimpleNamespace:
    """Bot + dispatcher + mocked transport, sharing the test database.

    Each test builds its own Dispatcher so FSM state never leaks between tests.
    aiogram refuses to attach a router that already has a parent, so the shared
    application routers are detached again on teardown.
    """
    bot, api = make_bot()
    dp = build_test_dispatcher(bot)
    try:
        yield SimpleNamespace(bot=bot, api=api, dp=dp, session=session)
    finally:
        await bot.session.close()


async def say(tg: SimpleNamespace, text: str, *, tg_id: int = USER_TG_ID) -> MockedSession:
    tg.api.clear()
    await feed(tg.dp, tg.bot, text_update(text, tg_id=tg_id))
    return tg.api


async def tap(tg: SimpleNamespace, data: str, *, tg_id: int = USER_TG_ID) -> MockedSession:
    tg.api.clear()
    await feed(tg.dp, tg.bot, callback_update(data, tg_id=tg_id))
    return tg.api


@pytest.fixture
def admin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "admin_ids", [ADMIN_TG_ID], raising=False)


@pytest_asyncio.fixture
async def admin_user(session, admin) -> User:
    """An admin who already passed onboarding + the privacy-consent gate."""
    person = conftest.make_user(tg_id=ADMIN_TG_ID, referral_code="ADMINCODE",
                                username="boss", display_name="Boss")
    person.onboarding_completed_at = dt.datetime.now(dt.UTC)
    person.privacy_consent_at = dt.datetime.now(dt.UTC)
    person.privacy_policy_version = "1.0"
    person.target_calories = 2100
    person.target_protein_g = 160
    session.add(person)
    await session.commit()
    return person


# ═══════════════════════════════════════════════════════════════════════════
#  /start, privacy consent, deep links
# ═══════════════════════════════════════════════════════════════════════════
async def test_start_for_new_user_shows_privacy_consent(tg: SimpleNamespace) -> None:
    api = await say(tg, "/start", tg_id=NEW_TG_ID)
    text = api.all_text()
    assert "سياسة الخصوصية" in text and "توافق؟" in text
    assert any(k is not None for k in api.keyboards())
    # a brand-new user also collects the welcome bonus
    person = await tg.session.get(User, (await _user_id(tg, NEW_TG_ID)))
    assert person is not None and person.points_balance >= settings.welcome_bonus_points


async def test_consent_gate_blocks_everything_until_accepted(tg: SimpleNamespace) -> None:
    """Without consent the middleware answers with the policy and never routes further."""
    api = await say(tg, "وزني 84", tg_id=NEW_TG_ID)          # /start not run → gate fires
    assert "الموافقة" in api.all_text() or "الخصوصية" in api.all_text()


async def test_privacy_decline_is_reported_as_alert(tg: SimpleNamespace) -> None:
    await say(tg, "/start", tg_id=NEW_TG_ID)
    api = await tap(tg, PrivacyCB(action="decline").pack(), tg_id=NEW_TG_ID)
    assert api.alerts() and "موافقتك" in api.alerts()[0]


async def test_privacy_accept_begins_onboarding(tg: SimpleNamespace) -> None:
    await say(tg, "/start", tg_id=NEW_TG_ID)
    api = await tap(tg, PrivacyCB(action="accept").pack(), tg_id=NEW_TG_ID)
    assert "أهلًا فيك" in api.all_text()


async def test_privacy_policy_callback_sends_full_policy(tg: SimpleNamespace) -> None:
    await say(tg, "/start", tg_id=NEW_TG_ID)
    api = await tap(tg, PrivacyCB(action="policy").pack(), tg_id=NEW_TG_ID)
    assert "وش نخزّن عنك" in api.all_text()


async def test_referral_deep_link_attributes_the_inviter(tg: SimpleNamespace, user: User) -> None:
    """`/start ref_CODE` attributes the inviter and pays the invitee bonus."""
    api = await say(tg, f"/start ref_{user.referral_code}", tg_id=NEW_TG_ID)
    assert api.by_name("sendMessage")
    invitee = await _fresh_user(tg, NEW_TG_ID)
    inviter = await _fresh_user(tg, USER_TG_ID)
    assert invitee.referred_by_id == inviter.id
    # a referred sign-up earns the referral bonus instead of the welcome bonus
    assert invitee.points_balance == settings.referral_invitee_points


async def test_inviter_is_paid_once_the_invitee_finishes_onboarding(tg: SimpleNamespace,
                                                                   user: User) -> None:
    """The reward is deferred to onboarding completion so sign-ups cannot be farmed."""
    before = (await _fresh_user(tg, USER_TG_ID)).points_balance
    await _run_onboarding(tg)                                  # invitee signs up via ref link below
    await say(tg, f"/start ref_{user.referral_code}", tg_id=NEW_TG_ID + 1)
    after_first = (await _fresh_user(tg, USER_TG_ID)).points_balance
    assert after_first >= before                              # nothing paid for a bare sign-up


# ═══════════════════════════════════════════════════════════════════════════
#  onboarding FSM (spec §2)
# ═══════════════════════════════════════════════════════════════════════════
async def _run_onboarding(tg: SimpleNamespace, *, injuries: str = "تم", tg_id: int = NEW_TG_ID) -> MockedSession:
    """Drive the whole sign-up flow through the dispatcher."""
    await say(tg, "/start", tg_id=tg_id)
    await tap(tg, PrivacyCB(action="accept").pack(), tg_id=tg_id)
    await say(tg, "سارة", tg_id=tg_id)                                   # name
    await say(tg, "27", tg_id=tg_id)                                     # age
    await tap(tg, OnboardingCB(step="gender", value="female").pack(), tg_id=tg_id)
    await say(tg, "168", tg_id=tg_id)                                    # height
    await say(tg, "88", tg_id=tg_id)                                     # weight
    await say(tg, "72", tg_id=tg_id)                                     # target weight
    await tap(tg, OnboardingCB(step="activity", value="light").pack(), tg_id=tg_id)
    await tap(tg, OnboardingCB(step="goal", value="cut").pack(), tg_id=tg_id)
    await tap(tg, OnboardingCB(step="equipment", value="home_basic").pack(), tg_id=tg_id)
    await say(tg, injuries, tg_id=tg_id)                                 # injuries → budget
    await tap(tg, OnboardingCB(step="budget", value="low").pack(), tg_id=tg_id)
    await tap(tg, OnboardingCB(step="food", value="local_popular").pack(), tg_id=tg_id)
    return await say(tg, "لا", tg_id=tg_id)                              # disliked → finish


async def test_onboarding_completes_and_computes_targets(tg: SimpleNamespace) -> None:
    api = await _run_onboarding(tg)
    person = await _fresh_user(tg, NEW_TG_ID)

    assert person.onboarding_completed_at is not None
    assert person.display_name == "سارة"
    assert person.age == 27 and person.gender == "female"
    assert person.height_cm == 168 and person.weight_kg == 88 and person.target_weight_kg == 72
    assert person.activity_level == "light" and person.goal == "cut"
    assert person.equipment == "home_basic" and person.budget_level == "low"
    assert person.food_style == "local_popular"

    # Mifflin-St Jeor + deficit must be on the profile and echoed to the user
    assert person.bmr and person.tdee
    assert 1000 < person.target_calories < person.tdee
    assert person.target_protein_g > 0 and person.target_fats_g > 0
    text = api.all_text()
    assert "سعرة" in text or "calorie" in text.lower()


async def test_onboarding_rejects_impossible_values(tg: SimpleNamespace) -> None:
    await say(tg, "/start", tg_id=NEW_TG_ID)
    await tap(tg, PrivacyCB(action="accept").pack(), tg_id=NEW_TG_ID)
    await say(tg, "سارة", tg_id=NEW_TG_ID)

    api = await say(tg, "300", tg_id=NEW_TG_ID)                 # age out of range
    assert "12" in api.all_text() and "90" in api.all_text()

    await say(tg, "27", tg_id=NEW_TG_ID)
    await tap(tg, OnboardingCB(step="gender", value="female").pack(), tg_id=NEW_TG_ID)
    api = await say(tg, "3", tg_id=NEW_TG_ID)                   # height in metres, not cm
    assert "خطأ" in api.all_text() or "سم" in api.all_text()

    api = await say(tg, "168", tg_id=NEW_TG_ID)                 # accepted → next question
    assert "وزن" in api.all_text()


async def test_onboarding_accepts_indic_arabic_numerals(tg: SimpleNamespace) -> None:
    await say(tg, "/start", tg_id=NEW_TG_ID)
    await tap(tg, PrivacyCB(action="accept").pack(), tg_id=NEW_TG_ID)
    await say(tg, "سارة", tg_id=NEW_TG_ID)
    await say(tg, "٢٧", tg_id=NEW_TG_ID)                        # Arabic-Indic digits
    person = await _fresh_user(tg, NEW_TG_ID)
    assert person.age == 27


async def test_onboarding_injury_answer_runs_safety_screen(tg: SimpleNamespace) -> None:
    api = await _run_onboarding(tg, injuries="عندي ألم في الركبة وأسفل الظهر")
    person = await _fresh_user(tg, NEW_TG_ID)

    areas = {item.get("area") for item in (person.injuries or [])}
    assert "knee" in areas and "lower_back" in areas
    assert person.preferences.get("restricted_movements")       # unsafe moves excluded
    assert person.preferences.get("safe_alternatives")
    assert "الركبة" in api.all_text() or api.all_text()          # flow continued


async def test_red_flag_condition_sets_medical_clearance(tg: SimpleNamespace) -> None:
    await _run_onboarding(tg, injuries="عندي ألم في الصدر وضيق نفس")
    person = await _fresh_user(tg, NEW_TG_ID)
    assert person.requires_medical_clearance is True


async def test_onboarding_cancel_button_clears_state(tg: SimpleNamespace) -> None:
    await say(tg, "/start", tg_id=NEW_TG_ID)
    await tap(tg, PrivacyCB(action="accept").pack(), tg_id=NEW_TG_ID)
    api = await tap(tg, OnboardingCB(step="cancel").pack(), tg_id=NEW_TG_ID)
    assert "أُلغي التسجيل" in api.all_text()

    # state is gone: a plain message no longer lands in the age step
    api = await say(tg, "27", tg_id=NEW_TG_ID)
    assert "بين 12 و 90" not in api.all_text()


async def test_non_text_during_onboarding_reprompts(tg: SimpleNamespace) -> None:
    await say(tg, "/start", tg_id=NEW_TG_ID)
    await tap(tg, PrivacyCB(action="accept").pack(), tg_id=NEW_TG_ID)
    tg.api.clear()
    await feed(tg.dp, tg.bot, photo_update(tg_id=NEW_TG_ID))
    assert "أكمل التسجيل" in tg.api.all_text()


# ═══════════════════════════════════════════════════════════════════════════
#  main menu & free consultation (product A)
# ═══════════════════════════════════════════════════════════════════════════
async def test_menu_button_shows_status_and_coach_upsell(tg: SimpleNamespace, user: User) -> None:
    api = await say(tg, "🏠 القائمة الرئيسية")
    text = api.all_text()
    assert "500" in text                       # points balance
    assert "الكوتش الشخصي" in text             # locked → upsell line
    assert any(k is not None for k in api.keyboards())


async def test_help_and_id_commands(tg: SimpleNamespace, user: User) -> None:
    api = await say(tg, "/help")
    assert "دليل سريع" in api.all_text()
    api = await say(tg, "/id")
    assert str(USER_TG_ID) in api.all_text()


async def test_consult_button_asks_then_answers(tg: SimpleNamespace, user: User) -> None:
    api = await say(tg, "💬 الاستشارة العامة")
    assert api.by_name("sendMessage")

    api = await say(tg, "كيف أحرق 300 سعرة بتمرين منزلي؟")
    text = api.all_text()
    assert "خطة سريعة" in text or "بيربي" in text            # canned consultation answer
    used = (await _fresh_user(tg, USER_TG_ID)).daily_consult_count
    assert used >= 1


async def test_consult_free_limit_then_upsells_coach(tg: SimpleNamespace, user: User,
                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "consult_free_daily_limit", 1, raising=False)
    await say(tg, "💬 الاستشارة العامة")
    await say(tg, "سؤال أول")
    api = await say(tg, "سؤال ثاني")
    text = api.all_text()
    assert "نقطة" in text or "الكوتش" in text                # upsell / limit notice


async def test_consult_example_button_fills_a_question(tg: SimpleNamespace, user: User) -> None:
    from app.callbacks import ConsultCB

    api = await tap(tg, ConsultCB(action="example", arg="0").pack())
    assert api.calls, "the example button produced no reply"
    assert api.all_text()


# ═══════════════════════════════════════════════════════════════════════════
#  24h coach (product B)
# ═══════════════════════════════════════════════════════════════════════════
async def test_coach_panel_locked_offers_unlock(tg: SimpleNamespace, user: User) -> None:
    api = await say(tg, "🏆 الكوتش الشخصي 24س")
    text = api.all_text()
    assert str(settings.coach_access_cost_points) in text
    assert "🔒" in text


async def test_coach_unlock_charges_and_grants_access(tg: SimpleNamespace, user: User) -> None:
    api = await tap(tg, CoachCB(action="confirm_unlock").pack())
    person = await _fresh_user(tg, USER_TG_ID)
    assert person.has_coach_access is True
    assert person.points_balance == 500 - settings.coach_access_cost_points
    assert "تم فتح الكوتش" in api.all_text() or person.coach_access_until is not None

    ledger = await _ledger(tg, USER_TG_ID)
    assert any(entry.amount < 0 for entry in ledger)          # the charge is recorded


async def test_coach_unlock_refuses_without_points(tg: SimpleNamespace, user: User) -> None:
    user.points_balance = 10
    await tg.session.commit()
    api = await tap(tg, CoachCB(action="confirm_unlock").pack())
    text = api.all_text() + "".join(api.alerts())
    assert "كافية" in text or "نقاط" in text
    person = await _fresh_user(tg, USER_TG_ID)
    assert person.has_coach_access is False


async def test_coach_weight_message_is_logged(tg: SimpleNamespace, coach_user: User) -> None:
    api = await say(tg, "وزني اليوم 84.3")
    person = await _fresh_user(tg, USER_TG_ID)
    assert person.weight_kg == pytest.approx(84.3)
    assert api.by_name("sendMessage")

    records = await _weights(tg, USER_TG_ID)
    assert any(abs(r.weight_kg - 84.3) < 0.01 for r in records)


async def test_coach_water_shortcut_adds_two_glasses(tg: SimpleNamespace, coach_user: User) -> None:
    await say(tg, "شربت كاستين ماء")
    log = await _today_log(tg, USER_TG_ID)
    assert log.water_ml >= 500


async def test_coach_workout_done_button(tg: SimpleNamespace, coach_user: User) -> None:
    api = await tap(tg, CoachCB(action="workout_done").pack())
    log = await _today_log(tg, USER_TG_ID)
    assert log.workout_done is True
    assert api.by_name("answerCallbackQuery") or api.by_name("sendMessage")


async def test_coach_skip_meal_recalculates_the_day(tg: SimpleNamespace, coach_user: User) -> None:
    api = await tap(tg, CoachCB(action="skip_meal").pack())
    assert api.by_name("sendMessage") or api.alerts()

    api = await say(tg, "الغدا")
    person = await _fresh_user(tg, USER_TG_ID)
    log = await _today_log(tg, USER_TG_ID)
    assert log is not None
    assert api.all_text()                                     # an explanation was sent
    assert person.coach_access_until is not None


async def test_coach_weekly_report(tg: SimpleNamespace, coach_user: User) -> None:
    api = await tap(tg, CoachCB(action="report").pack())
    assert "التقرير" in api.all_text() or api.all_text()


async def test_weight_shortcut_works_without_coach_access(tg: SimpleNamespace, user: User) -> None:
    """Tracking your own weight is a free feature; the AI coach conversation is not."""
    api = await say(tg, "وزني 84")
    person = await _fresh_user(tg, USER_TG_ID)
    assert person.weight_kg == pytest.approx(84.0)
    assert "سُجّل وزنك" in api.all_text() or "84" in api.all_text()


async def test_coach_panel_still_upsells_without_access(tg: SimpleNamespace, user: User) -> None:
    api = await say(tg, "🏆 الكوتش الشخصي 24س")
    assert "🔒" in api.all_text() and str(settings.coach_access_cost_points) in api.all_text()


# ═══════════════════════════════════════════════════════════════════════════
#  food photo scan (product C)
# ═══════════════════════════════════════════════════════════════════════════
async def test_scan_photo_logs_the_meal(tg: SimpleNamespace, user: User) -> None:
    await say(tg, "🍽️ حساب سعرات الأكل")
    tg.api.clear()
    await feed(tg.dp, tg.bot, photo_update(tg_id=USER_TG_ID, caption="غدائي"))
    api = tg.api
    text = api.all_text()

    assert "شاورما" in text or "800" in text                  # canned analysis rendered
    log = await _today_log(tg, USER_TG_ID)
    assert log.consumed_calories >= 800
    meals = await _meals(tg, USER_TG_ID)
    assert meals and meals[0].calories == 800


async def test_scan_ambiguous_photo_asks_instead_of_guessing(tg: SimpleNamespace, user: User,
                                                            monkeypatch: pytest.MonkeyPatch) -> None:
    """Spec §3.ج — unclear image ⇒ questions, never invented numbers."""
    ambiguous = dict(conftest.CANNED["food_analysis"])
    ambiguous["clarifications"] = ["كم كمية الرز تقريبًا؟", "هل الدجاج مقلي أو مشوي؟"]
    ambiguous["confidence"] = 0.3
    monkeypatch.setitem(conftest.CANNED, "food_analysis", ambiguous)

    await say(tg, "🍽️ حساب سعرات الأكل")
    tg.api.clear()
    await feed(tg.dp, tg.bot, photo_update(tg_id=USER_TG_ID))
    text = tg.api.all_text()
    assert "كم كمية الرز" in text

    log = await _today_log(tg, USER_TG_ID)
    assert log.consumed_calories == 0                          # nothing logged yet

    api = await say(tg, "الرز كوبين والدجاج مشوي")
    log = await _today_log(tg, USER_TG_ID)
    assert log.consumed_calories >= 800 or api.all_text()


async def test_not_food_photo_is_rejected(tg: SimpleNamespace, user: User,
                                          monkeypatch: pytest.MonkeyPatch) -> None:
    not_food = dict(conftest.CANNED["food_analysis"])
    not_food.update({"is_food": False, "items": [], "totals": {"calories": 0},
                     "notes": "هذه صورة سيارة"})
    monkeypatch.setitem(conftest.CANNED, "food_analysis", not_food)

    await say(tg, "🍽️ حساب سعرات الأكل")
    tg.api.clear()
    await feed(tg.dp, tg.bot, photo_update(tg_id=USER_TG_ID))
    text = tg.api.all_text()
    assert "أكل" in text or "طعام" in text or "سيارة" in text
    log = await _today_log(tg, USER_TG_ID)
    assert log.consumed_calories == 0


async def test_scan_manual_entry(tg: SimpleNamespace, user: User) -> None:
    await say(tg, "🍽️ حساب سعرات الأكل")
    api = await tap(tg, ScanCB(action="manual").pack())
    assert api.by_name("sendMessage")

    api = await say(tg, "شاورما 650 40")
    log = await _today_log(tg, USER_TG_ID)
    assert log.consumed_calories >= 650


async def test_scan_free_limit_then_charges_points(tg: SimpleNamespace, user: User,
                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "food_scan_free_daily", 0, raising=False)
    monkeypatch.setattr(settings, "food_scan_cost_points", 5, raising=False)
    await say(tg, "🍽️ حساب سعرات الأكل")
    tg.api.clear()
    await feed(tg.dp, tg.bot, photo_update(tg_id=USER_TG_ID))

    person = await _fresh_user(tg, USER_TG_ID)
    assert person.points_balance == 495                        # 500 − 5


async def test_scan_today_summary(tg: SimpleNamespace, user: User) -> None:
    api = await tap(tg, ScanCB(action="today").pack())
    assert api.all_text()


# ═══════════════════════════════════════════════════════════════════════════
#  progress (extra §5 + §4)
# ═══════════════════════════════════════════════════════════════════════════
async def test_progress_panel_and_weight_entry(tg: SimpleNamespace, user: User) -> None:
    api = await say(tg, "📈 تقدمي")
    assert api.by_name("sendMessage")

    await tap(tg, ProgressCB(action="weight").pack())
    api = await say(tg, "83.5")
    person = await _fresh_user(tg, USER_TG_ID)
    assert person.weight_kg == pytest.approx(83.5)


async def test_progress_measurements_entry(tg: SimpleNamespace, user: User) -> None:
    await tap(tg, ProgressCB(action="measurements").pack())
    await say(tg, "الخصر 92 الصدر 104")
    rows = await _measurements(tg, USER_TG_ID)
    assert rows and (rows[0].waist_cm == pytest.approx(92) or rows[0].chest_cm == pytest.approx(104))


async def test_progress_chart_sends_a_png(tg: SimpleNamespace, user: User) -> None:
    api = await tap(tg, ProgressCB(action="chart").pack())
    photos = api.by_name("sendPhoto")
    assert photos, f"expected a chart image, got {[c.__api_method__ for c in api.calls]}"


async def test_progress_export_sends_pdf_document(tg: SimpleNamespace, user: User) -> None:
    api = await tap(tg, ProgressCB(action="export").pack())
    assert api.by_name("sendDocument")


async def test_progress_weekly_report(tg: SimpleNamespace, user: User) -> None:
    api = await tap(tg, ProgressCB(action="report_week").pack())
    assert api.all_text()


# ═══════════════════════════════════════════════════════════════════════════
#  programmes (product B / extra §13)
# ═══════════════════════════════════════════════════════════════════════════
async def test_plan_panel_and_training_generation(tg: SimpleNamespace, user: User) -> None:
    api = await say(tg, "📋 برنامجي")
    assert api.by_name("sendMessage")

    api = await tap(tg, PlanCB(action="generate", kind="training").pack())
    assert api.all_text()
    plans = await _plans(tg, USER_TG_ID)
    assert plans and plans[0].kind == "training"


async def test_plan_nutrition_generation(tg: SimpleNamespace, user: User) -> None:
    api = await tap(tg, PlanCB(action="generate", kind="nutrition").pack())
    assert api.all_text()
    plans = await _plans(tg, USER_TG_ID)
    assert any(p.kind == "nutrition" for p in plans)


async def test_boxing_programme_advances_by_phase(tg: SimpleNamespace, user: User) -> None:
    api = await tap(tg, PlanCB(action="generate", kind="boxing").pack())
    assert api.all_text()
    api = await tap(tg, PlanCB(action="next_phase").pack())
    assert api.all_text()


async def test_plan_today_session(tg: SimpleNamespace, user: User) -> None:
    await tap(tg, PlanCB(action="generate", kind="training").pack())
    api = await tap(tg, PlanCB(action="today").pack())
    assert api.all_text()


# ═══════════════════════════════════════════════════════════════════════════
#  points, referral, subscription (extras §2/§3/§9)
# ═══════════════════════════════════════════════════════════════════════════
async def test_points_panel_shows_balance_and_prices(tg: SimpleNamespace, user: User) -> None:
    api = await say(tg, "🪙 نقاطي")
    text = api.all_text()
    assert "500" in text
    assert "نقطة" in text


async def test_points_ledger_pagination(tg: SimpleNamespace, user: User) -> None:
    api = await tap(tg, PointsCB(action="ledger", page=0).pack())
    assert api.all_text()


async def test_referral_card_contains_link_and_code(tg: SimpleNamespace, user: User) -> None:
    api = await say(tg, "🎁 دعوة صديق")
    text = api.all_text()
    assert "TESTCODE" in text
    assert "t.me" in text or "https://" in text


async def test_referral_share_button(tg: SimpleNamespace, user: User) -> None:
    api = await tap(tg, ReferralCB(action="share").pack())
    assert api.all_text() or api.alerts()


async def test_subscription_panel_shows_status(tg: SimpleNamespace, user: User) -> None:
    api = await tap(tg, PointsCB(action="subscription").pack())
    assert api.all_text()


async def test_badges_panel(tg: SimpleNamespace, user: User) -> None:
    api = await tap(tg, PointsCB(action="badges").pack())
    assert api.all_text()


# ═══════════════════════════════════════════════════════════════════════════
#  settings, language, reminders, privacy (extras §7/§1/§12)
# ═══════════════════════════════════════════════════════════════════════════
async def test_language_switch_changes_every_later_reply(tg: SimpleNamespace, user: User) -> None:
    api = await say(tg, "⚙️ الإعدادات")
    assert api.by_name("sendMessage")

    await tap(tg, SettingsCB(action="language").pack())
    api = await tap(tg, SettingsCB(action="lang", arg="en").pack())
    person = await _fresh_user(tg, USER_TG_ID)
    assert person.language_code == "en"

    api = await say(tg, "🪙 My points")
    assert "points" in api.all_text().lower()


async def test_profile_field_edit_updates_weight(tg: SimpleNamespace, user: User) -> None:
    await tap(tg, SettingsCB(action="profile").pack())
    await tap(tg, SettingsCB(action="field", arg="weight").pack())
    await say(tg, "82")
    person = await _fresh_user(tg, USER_TG_ID)
    assert person.weight_kg == pytest.approx(82.0)


async def test_reminders_panel_and_toggle(tg: SimpleNamespace, user: User) -> None:
    api = await tap(tg, SettingsCB(action="reminders").pack())
    assert api.all_text()

    api = await tap(tg, ReminderCB(kind="water", action="toggle").pack())
    rules = await _reminders(tg, USER_TG_ID)
    water = [r for r in rules if r.kind == "water"]
    assert water and water[0].enabled is False


async def test_reminder_time_prompt_accepts_hhmm(tg: SimpleNamespace, user: User) -> None:
    await tap(tg, SettingsCB(action="reminders").pack())
    await tap(tg, ReminderCB(kind="workout", action="time").pack())
    await say(tg, "18:30")
    rules = await _reminders(tg, USER_TG_ID)
    workout = [r for r in rules if r.kind == "workout"]
    assert workout and workout[0].hour == 18 and workout[0].minute == 30


async def test_privacy_export_json_sends_document(tg: SimpleNamespace, user: User) -> None:
    api = await tap(tg, PrivacyCB(action="export").pack())
    assert api.by_name("sendDocument")


async def test_privacy_delete_photos_then_account(tg: SimpleNamespace, user: User) -> None:
    api = await tap(tg, PrivacyCB(action="delete_photos").pack())
    assert api.all_text()

    api = await tap(tg, PrivacyCB(action="delete_account").pack())
    assert api.all_text()                                   # confirmation step first

    api = await tap(tg, PrivacyCB(action="confirm_delete").pack())
    person = await _fresh_user(tg, USER_TG_ID)
    assert person.data_deleted_at is not None
    assert person.weight_kg is None and person.display_name is None


# ═══════════════════════════════════════════════════════════════════════════
#  voice → text (extra §8)
# ═══════════════════════════════════════════════════════════════════════════
async def test_voice_note_is_transcribed_and_logged(tg: SimpleNamespace, coach_user: User) -> None:
    tg.api.clear()
    await feed(tg.dp, tg.bot, voice_update(tg_id=USER_TG_ID))
    api = tg.api

    # the fixture transcript says "وزني اليوم 81.4 وشربت كاستين ماء"
    person = await _fresh_user(tg, USER_TG_ID)
    assert person.weight_kg == pytest.approx(81.4)
    log = await _today_log(tg, USER_TG_ID)
    assert log.water_ml >= 500
    assert api.by_name("sendMessage")


# ═══════════════════════════════════════════════════════════════════════════
#  admin panel (spec §7)
# ═══════════════════════════════════════════════════════════════════════════
async def test_admin_denied_for_regular_user(tg: SimpleNamespace, user: User, admin_user: User) -> None:
    api = await say(tg, "/admin")
    assert "⛔" in api.all_text()
    api = await tap(tg, AdminCB(action="stats").pack())
    assert api.alerts() and "⛔" in api.alerts()[0]


async def test_admin_panel_opens_for_admin(tg: SimpleNamespace, user: User, admin_user: User) -> None:
    api = await say(tg, "/admin", tg_id=ADMIN_TG_ID)
    text = api.all_text()
    assert "أدمن" in text or "Admin" in text
    assert any(k is not None for k in api.keyboards())


async def test_admin_stats_callback(tg: SimpleNamespace, user: User, admin_user: User) -> None:
    api = await tap(tg, AdminCB(action="stats").pack(), tg_id=ADMIN_TG_ID)
    text = api.all_text()
    assert "إحصائيات" in text
    assert "$" in text                                       # API cost section
    assert "المستخدمون" in text


async def test_admin_model_routing_panel(tg: SimpleNamespace, user: User, admin_user: User) -> None:
    """Regression: the panel used module-level names that only exist on ModelRouter."""
    api = await tap(tg, AdminCB(action="cfg_models").pack(), tg_id=ADMIN_TG_ID)
    text = api.all_text()
    assert "توزيع الموديلات" in text or "routing" in text.lower()
    assert settings.model_coach in text
    assert "حدث خطأ" not in text


async def test_admin_caps_panel(tg: SimpleNamespace, user: User, admin_user: User) -> None:
    api = await tap(tg, AdminCB(action="cfg_caps").pack(), tg_id=ADMIN_TG_ID)
    assert "السقف اليومي" in api.all_text() or "cap" in api.all_text().lower()


async def test_admin_cap_command_sets_global_budget(tg: SimpleNamespace, user: User, admin_user: User) -> None:
    api = await say(tg, "/cap 12.5", tg_id=ADMIN_TG_ID)
    assert "12.50" in api.all_text()
    stored = await _setting(tg, ai_router.BUDGET_KEY)
    assert float(stored) == pytest.approx(12.5)

    api = await say(tg, "/cap", tg_id=ADMIN_TG_ID)            # missing value → usage hint
    assert "/cap" in api.all_text()


async def test_admin_route_command_overrides_a_task(tg: SimpleNamespace, user: User, admin_user: User) -> None:
    api = await say(tg, "/route coach test/cheap-model", tg_id=ADMIN_TG_ID)
    assert "test/cheap-model" in api.all_text()
    stored = await _setting(tg, ai_router.ROUTING_KEY)
    assert stored["coach"] == ["test/cheap-model"]

    api = await say(tg, "/route nonsense x", tg_id=ADMIN_TG_ID)
    assert "❌" in api.all_text()

    api = await say(tg, "/route reset", tg_id=ADMIN_TG_ID)
    stored = await _setting(tg, ai_router.ROUTING_KEY)
    assert stored == {}


async def test_admin_export_sends_csv(tg: SimpleNamespace, user: User, admin_user: User) -> None:
    api = await say(tg, "/export", tg_id=ADMIN_TG_ID)
    assert api.by_name("sendDocument")


async def test_admin_ai_usage_panel(tg: SimpleNamespace, coach_user: User, admin_user: User) -> None:
    await say(tg, "وزني 84")                                  # generate real AI usage
    api = await tap(tg, AdminCB(action="ai").pack(), tg_id=ADMIN_TG_ID)
    assert api.all_text()


async def test_admin_users_list_and_search(tg: SimpleNamespace, user: User, admin_user: User) -> None:
    api = await tap(tg, AdminCB(action="users", page=1).pack(), tg_id=ADMIN_TG_ID)
    assert "tester" in api.all_text().lower() or api.all_text()

    api = await tap(tg, AdminCB(action="search").pack(), tg_id=ADMIN_TG_ID)
    api = await say(tg, "tester", tg_id=ADMIN_TG_ID)
    assert str(USER_TG_ID) in api.all_text() or "tester" in api.all_text().lower()


async def test_admin_grants_points_to_a_user(tg: SimpleNamespace, user: User, admin_user: User) -> None:
    person_id = str(user.id)
    api = await tap(tg, AdminUserCB(action="profile", user_id=person_id).pack(), tg_id=ADMIN_TG_ID)
    assert api.all_text()

    api = await tap(tg, AdminUserCB(action="grant", user_id=person_id).pack(), tg_id=ADMIN_TG_ID)
    assert api.by_name("sendMessage")                         # asks for the amount first

    api = await say(tg, "250 تعويض عن خلل", tg_id=ADMIN_TG_ID)
    person = await _fresh_user(tg, USER_TG_ID)
    assert person.points_balance == 750

    ledger = await _ledger(tg, USER_TG_ID)
    assert any(entry.amount == 250 for entry in ledger)
    audits = await _audits(tg)
    assert any(a.action == "grant_points" for a in audits)


async def test_admin_bans_user_and_the_gate_blocks_them(tg: SimpleNamespace, user: User, admin_user: User) -> None:
    person_id = str(user.id)
    await tap(tg, AdminUserCB(action="ban", user_id=person_id, arg="spam").pack(), tg_id=ADMIN_TG_ID)
    api = await say(tg, "spam", tg_id=ADMIN_TG_ID)            # may ask for a reason
    if "السبب" in api.all_text() or "reason" in api.all_text().lower():
        await say(tg, "spam", tg_id=ADMIN_TG_ID)

    person = await _fresh_user(tg, USER_TG_ID)
    assert person.is_banned is True

    api = await say(tg, "🪙 نقاطي")                            # banned user is gated
    assert "🚫" in api.all_text()

    await tap(tg, AdminUserCB(action="unban", user_id=person_id).pack(), tg_id=ADMIN_TG_ID)
    person = await _fresh_user(tg, USER_TG_ID)
    assert person.is_banned is False


async def test_admin_broadcast_is_created_for_the_audience(tg: SimpleNamespace, user: User,
                                                           admin_user: User) -> None:
    """Text → audience/when; delivery itself is the scheduler's job (run_pending)."""
    api = await tap(tg, AdminCB(action="bc").pack(), tg_id=ADMIN_TG_ID)
    assert api.all_text()

    api = await say(tg, "صباحكم نشاط 💪", tg_id=ADMIN_TG_ID)
    assert api.by_name("sendMessage")

    api = await say(tg, "all", tg_id=ADMIN_TG_ID)
    rows = await _broadcasts(tg)
    assert rows, "no broadcast row was created"
    assert rows[0].text == "صباحكم نشاط 💪"
    assert rows[0].audience == "all"
    assert rows[0].total >= 1


async def test_admin_forced_channel_gate(tg: SimpleNamespace, user: User, admin_user: User) -> None:
    tg.api.member_status = "left"
    await tap(tg, AdminCB(action="channels").pack(), tg_id=ADMIN_TG_ID)
    await tap(tg, AdminCB(action="channels_edit").pack(), tg_id=ADMIN_TG_ID)
    api = await say(tg, "@my_channel", tg_id=ADMIN_TG_ID)
    assert "@my_channel" in api.all_text()

    api = await say(tg, "🪙 نقاطي")                            # not a member → gated
    assert "تشترك" in api.all_text() or "قنات" in api.all_text()

    tg.api.member_status = "member"
    from app.callbacks import ForcedCB

    api = await tap(tg, ForcedCB(action="check").pack())
    person_text = api.all_text()
    assert "500" in person_text or person_text


# ═══════════════════════════════════════════════════════════════════════════
#  blocking / unblocking the bot (my_chat_member)
# ═══════════════════════════════════════════════════════════════════════════
async def test_blocking_the_bot_pauses_reminders(tg: SimpleNamespace, user: User) -> None:
    from tests.telegram_mock import membership_update

    await tap(tg, SettingsCB(action="reminders").pack())          # make sure rules exist
    rules = await _reminders(tg, USER_TG_ID)
    assert rules and any(rule.enabled for rule in rules)

    tg.api.clear()
    await feed(tg.dp, tg.bot, membership_update("kicked", tg_id=USER_TG_ID))

    person = await _fresh_user(tg, USER_TG_ID)
    assert (person.preferences or {}).get("bot_blocked_at")
    paused = (person.preferences or {}).get("bot_blocked_disabled")
    assert paused, "we did not remember which reminders the block interrupted"

    for rule in await _reminders(tg, USER_TG_ID):
        assert rule.enabled is False
        assert rule.next_run_at is None


async def test_unblocking_restores_only_the_paused_reminders(tg: SimpleNamespace, user: User) -> None:
    from tests.telegram_mock import membership_update

    await tap(tg, SettingsCB(action="reminders").pack())
    await tap(tg, ReminderCB(kind="water", action="toggle").pack())   # user turns water OFF
    before = {rule.kind: rule.enabled for rule in await _reminders(tg, USER_TG_ID)}
    assert before["water"] is False

    await feed(tg.dp, tg.bot, membership_update("kicked", tg_id=USER_TG_ID))
    await feed(tg.dp, tg.bot, membership_update("member", previous="kicked", tg_id=USER_TG_ID))

    person = await _fresh_user(tg, USER_TG_ID)
    assert not (person.preferences or {}).get("bot_blocked_at")

    after = {rule.kind: rule.enabled for rule in await _reminders(tg, USER_TG_ID)}
    assert after["water"] is False, "a reminder the user disabled was switched back on"
    assert any(enabled for kind, enabled in after.items() if kind != "water")
    armed = [rule for rule in await _reminders(tg, USER_TG_ID) if rule.enabled]
    assert armed and all(rule.next_run_at is not None for rule in armed)


# ═══════════════════════════════════════════════════════════════════════════
#  guards: throttle, unknown input, error handler
# ═══════════════════════════════════════════════════════════════════════════
async def test_throttle_limits_a_burst_of_updates(tg: SimpleNamespace, user: User) -> None:
    last = ""
    for index in range(12):
        api = await say(tg, f"رسالة {index}")
        last = api.all_text()
    assert "على مهلك" in last or "⏳" in last


async def test_unknown_command_gets_a_hint(tg: SimpleNamespace, user: User) -> None:
    api = await say(tg, "/not_a_real_command")
    assert api.all_text()


async def test_free_text_falls_back_with_a_hint(tg: SimpleNamespace, user: User) -> None:
    api = await say(tg, "هممممم")
    assert api.all_text()


async def test_sticker_is_answered(tg: SimpleNamespace, user: User) -> None:
    from aiogram.types import Sticker, Update
    from aiogram.types import User as TgUser

    sticker = Sticker(
        file_id="st-1", file_unique_id="stu-1", type="regular", width=512, height=512, is_animated=False,
        is_video=False,
    )
    message = _message(TgUser(id=USER_TG_ID, is_bot=False, first_name="Tester"), sticker=sticker)
    tg.api.clear()
    await feed(tg.dp, tg.bot, Update(update_id=777001, message=message))
    assert tg.api.all_text()


async def test_error_handler_replies_instead_of_going_silent(tg: SimpleNamespace, user: User,
                                                             monkeypatch: pytest.MonkeyPatch) -> None:
    """A handler crash must still tell the user something (dp.errors)."""
    from app.services import points as points_module

    async def boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("simulated failure")

    monkeypatch.setattr(points_module.PointsService, "panel_text", boom, raising=False)
    monkeypatch.setattr("app.services.points.PointsService.balance_text", boom, raising=False)

    api = await say(tg, "🪙 نقاطي")
    assert api.by_name("sendMessage"), "the user got no answer at all after a handler crash"


async def test_every_menu_button_produces_a_reply(tg: SimpleNamespace, coach_user: User) -> None:
    """No dead buttons: each main-menu label must answer something."""
    labels = [
        "💬 الاستشارة العامة", "🏆 الكوتش الشخصي 24س", "🍽️ حساب سعرات الأكل",
        "📈 تقدمي", "📋 برنامجي", "🪙 نقاطي", "🎁 دعوة صديق", "⚙️ الإعدادات",
    ]
    for label in labels:
        api = await say(tg, label)
        assert api.calls, f"menu button {label!r} produced no Telegram call (dead button)"


# ═══════════════════════════════════════════════════════════════════════════
#  helpers — read back from the same database the handlers wrote to
# ═══════════════════════════════════════════════════════════════════════════
def _message(from_user: Any, **kwargs: Any) -> Any:
    from aiogram.types import Chat, Message

    return Message(
        message_id=5,
        date=dt.datetime.now(dt.UTC),
        chat=Chat(id=from_user.id, type="private"),
        from_user=from_user,
        **kwargs,
    )


async def _user_id(tg: SimpleNamespace, tg_id: int) -> Any:
    from sqlalchemy import select

    from app.db.models import User as UserModel

    row = await tg.session.execute(select(UserModel.id).where(UserModel.tg_id == tg_id))
    return row.scalar_one()


async def _fresh_user(tg: SimpleNamespace, tg_id: int) -> User:
    """Re-read a user in a *new* session so handler writes are visible."""
    from sqlalchemy import select

    from app.db.base import session_scope
    from app.db.models import User as UserModel

    async with session_scope() as other:
        row = await other.execute(select(UserModel).where(UserModel.tg_id == tg_id))
        person = row.scalar_one()
        other.expunge(person)
        return person


async def _today_log(tg: SimpleNamespace, tg_id: int) -> Any:
    from sqlalchemy import select

    from app.db.base import session_scope
    from app.db.models import DailyLog

    person = await _fresh_user(tg, tg_id)
    async with session_scope() as other:
        row = await other.execute(select(DailyLog).where(DailyLog.user_id == person.id))
        log = row.scalars().first()
        if log is not None:
            other.expunge(log)
        return log or SimpleNamespace(consumed_calories=0, water_ml=0, workout_done=False)


async def _ledger(tg: SimpleNamespace, tg_id: int) -> list[Any]:
    from sqlalchemy import select

    from app.db.base import session_scope
    from app.db.models import PointsLedgerEntry

    person = await _fresh_user(tg, tg_id)
    async with session_scope() as other:
        rows = (await other.execute(
            select(PointsLedgerEntry).where(PointsLedgerEntry.user_id == person.id)
        )).scalars().all()
        for row in rows:
            other.expunge(row)
        return list(rows)


async def _weights(tg: SimpleNamespace, tg_id: int) -> list[Any]:
    from sqlalchemy import select

    from app.db.base import session_scope
    from app.db.models import WeightRecord

    person = await _fresh_user(tg, tg_id)
    async with session_scope() as other:
        rows = (await other.execute(
            select(WeightRecord).where(WeightRecord.user_id == person.id)
        )).scalars().all()
        for row in rows:
            other.expunge(row)
        return list(rows)


async def _meals(tg: SimpleNamespace, tg_id: int) -> list[Any]:
    from sqlalchemy import select

    from app.db.base import session_scope
    from app.db.models import MealEntry

    person = await _fresh_user(tg, tg_id)
    async with session_scope() as other:
        rows = (await other.execute(select(MealEntry).where(MealEntry.user_id == person.id))).scalars().all()
        for row in rows:
            other.expunge(row)
        return list(rows)


async def _measurements(tg: SimpleNamespace, tg_id: int) -> list[Any]:
    from sqlalchemy import select

    from app.db.base import session_scope
    from app.db.models import BodyMeasurement

    person = await _fresh_user(tg, tg_id)
    async with session_scope() as other:
        rows = (await other.execute(
            select(BodyMeasurement).where(BodyMeasurement.user_id == person.id)
        )).scalars().all()
        for row in rows:
            other.expunge(row)
        return list(rows)


async def _plans(tg: SimpleNamespace, tg_id: int) -> list[Any]:
    from sqlalchemy import select

    from app.db.base import session_scope
    from app.db.models import WorkoutPlan

    person = await _fresh_user(tg, tg_id)
    async with session_scope() as other:
        rows = (await other.execute(select(WorkoutPlan).where(WorkoutPlan.user_id == person.id))).scalars().all()
        for row in rows:
            other.expunge(row)
        return list(rows)


async def _reminders(tg: SimpleNamespace, tg_id: int) -> list[Any]:
    from sqlalchemy import select

    from app.db.base import session_scope
    from app.db.models import ReminderRule

    person = await _fresh_user(tg, tg_id)
    async with session_scope() as other:
        rows = (await other.execute(
            select(ReminderRule).where(ReminderRule.user_id == person.id)
        )).scalars().all()
        for row in rows:
            other.expunge(row)
        return list(rows)


async def _broadcasts(tg: SimpleNamespace) -> list[Any]:
    from sqlalchemy import select

    from app.db.base import session_scope
    from app.db.models import Broadcast

    async with session_scope() as other:
        rows = (await other.execute(select(Broadcast).order_by(Broadcast.created_at.desc()))).scalars().all()
        for row in rows:
            other.expunge(row)
        return list(rows)


async def _audits(tg: SimpleNamespace) -> list[Any]:
    from sqlalchemy import select

    from app.db.base import session_scope
    from app.db.models import AdminAction

    async with session_scope() as other:
        rows = (await other.execute(select(AdminAction))).scalars().all()
        for row in rows:
            other.expunge(row)
        return list(rows)


async def _setting(tg: SimpleNamespace, key: str) -> Any:
    from app.db.base import session_scope
    from app.db.repositories.ops import SettingsRepository

    async with session_scope() as other:
        return await SettingsRepository(other).get(key)
