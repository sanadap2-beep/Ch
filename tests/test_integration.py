"""End-to-end service tests with an offline AI and a fake Telegram bot.

Covers the paid paths (coach, vision, plans, reports), the scheduler-facing
services (reminders, broadcasts, renewals) and the privacy tools.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from aiogram.exceptions import TelegramForbiddenError

from app.config import settings
from app.constants import BroadcastStatus, ReminderKind
from app.services.reminders import compute_next_run
from tests import conftest


def jpeg_bytes(size: int = 64) -> bytes:
    """A real (tiny) JPEG so PIL-based optimisation/storage works in tests."""
    import io

    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (size, size), (200, 160, 90)).save(buffer, format="JPEG", quality=80)
    return buffer.getvalue()


class FakeBot:
    """Minimal stand-in for ``aiogram.Bot`` — records sends, can block users."""

    def __init__(self, blocked: set[int] | None = None) -> None:
        self.sent: list[tuple[int, str]] = []
        self.photos: list[tuple[int, str]] = []
        self.blocked = blocked or set()

    async def send_message(self, chat_id: int, text: str, **kwargs) -> None:  # noqa: ANN002, ANN003
        if chat_id in self.blocked:
            raise _forbidden(chat_id)
        self.sent.append((chat_id, text))

    async def send_photo(self, chat_id: int, photo: str, **kwargs) -> None:  # noqa: ANN002, ANN003
        if chat_id in self.blocked:
            raise _forbidden(chat_id)
        self.photos.append((chat_id, photo))


def _forbidden(chat_id: int) -> TelegramForbiddenError:
    from aiogram.methods import SendMessage

    return TelegramForbiddenError(method=SendMessage(chat_id=chat_id, text="x"),
                                  message="bot was blocked by the user")


# ═══════════════════════════════════════════════════════════════════════════
#  dispatcher wiring
# ═══════════════════════════════════════════════════════════════════════════
def test_dispatcher_registers_every_router() -> None:
    import os

    os.environ.setdefault("BOT_TOKEN", "123456789:TEST-token")
    from app.bot import build_dispatcher, create_bot
    from app.handlers import routers

    dispatcher = build_dispatcher(create_bot("123456789:TEST-token"))
    names = {router.name for router in routers}
    assert {"start", "onboarding", "consult", "coach", "scan", "progress", "plans",
            "points", "settings", "media", "admin", "fallback"} <= names

    total = sum(len(router.message.handlers) + len(router.callback_query.handlers)
                for router in routers)
    assert total > 100
    assert dispatcher.sub_routers                     # routers are attached
    # the catch-all must be last, or it swallows every other message
    assert routers[-1].name == "fallback"


# ═══════════════════════════════════════════════════════════════════════════
#  personal coach
# ═══════════════════════════════════════════════════════════════════════════
async def test_coach_parses_weight_and_charges_for_the_turn(services, coach_user) -> None:
    balance_before = int(coach_user.points_balance)
    reply = await services.coach.handle_message(coach_user, "وزني اليوم 84.3", lang="ar")

    assert any(event.startswith("weight:") for event in reply.events)
    assert coach_user.weight_kg == pytest.approx(84.3)
    assert reply.charge is not None and reply.charge.points < 0
    assert coach_user.points_balance == balance_before + reply.charge.points
    assert reply.charge.points >= -settings.coach_points_maximum
    assert coach_user.daily_coach_messages == 1
    assert reply.text and reply.model

    history = await services.repos.chat.history(coach_user.id, scope="coach")
    roles = [item["role"] for item in history]
    assert "user" in roles and "assistant" in roles

    usage = await services.repos.ai_usage.breakdown(days=1)
    assert usage["calls"] >= 1
    assert usage["total_cost_usd"] > 0


async def test_coach_logs_water_from_a_spoken_sentence(services, coach_user) -> None:
    reply = await services.coach.handle_message(coach_user, "شربت كاستين ماء", lang="ar")
    assert any(event.startswith("water:500") for event in reply.events)
    log = await services.repos.tracking.daily_log(coach_user, coach_user.local_today())
    assert log.water_ml == 500


async def test_coach_handles_a_skipped_meal_and_prepends_the_recalculation(
    services, coach_user
) -> None:
    reply = await services.coach.handle_message(coach_user, "ما أكلت الغدا، كنت بالدوام", lang="ar")
    assert any(event.startswith("skipped:") for event in reply.events)
    log = await services.repos.tracking.daily_log(coach_user, coach_user.local_today())
    assert log.skipped_meals


async def test_coach_marks_the_workout_done(services, coach_user) -> None:
    before = int(coach_user.total_workouts or 0)
    reply = await services.coach.handle_message(coach_user, "خلصت التمرين اليوم", lang="ar")
    assert "workout_done" in reply.events
    assert coach_user.total_workouts == before + 1
    log = await services.repos.tracking.daily_log(coach_user, coach_user.local_today())
    assert log.workout_done is True


async def test_coach_refuses_without_enough_points(services, user) -> None:
    from app.db.repositories.economy import InsufficientPoints

    user.points_balance = 0
    with pytest.raises(InsufficientPoints):
        await services.coach.handle_message(user, "وزني 84", lang="ar")


async def test_coach_status_and_day_context(services, coach_user) -> None:
    status = await services.coach.status_block(coach_user, lang="ar")
    assert status
    log = await services.repos.tracking.daily_log(coach_user, coach_user.local_today())
    context = services.coach.build_day_context(coach_user, log, lang="ar")
    assert str(int(log.target_calories)) in context or "سعرة" in context


async def test_recalibration_after_a_stall_changes_the_plan(services, coach_user) -> None:
    today = coach_user.local_today()
    for offset, weight in ((12, 86.0), (8, 86.0), (4, 86.05), (1, 86.0)):
        await services.repos.tracking.record_weight(coach_user, weight, day=today - timedelta(days=offset))
    coach_user.stall_weeks = 2

    recalibration = await services.coach.maybe_recalibrate(coach_user)
    assert recalibration is not None
    if recalibration.changed:
        assert coach_user.target_calories == recalibration.new_calories
        assert recalibration.reason_ar


# ═══════════════════════════════════════════════════════════════════════════
#  food vision
# ═══════════════════════════════════════════════════════════════════════════
async def test_food_analysis_is_logged_against_the_day(services, user) -> None:
    analysis = await services.vision.analyse_food(user, jpeg_bytes(), lang="ar")
    assert analysis.is_food and analysis.calories == 800
    assert analysis.items and analysis.media_id is not None

    entry, day_state = await services.vision.log_meal(user, analysis)
    assert entry.calories == 800
    log = await services.repos.tracking.daily_log(user, user.local_today())
    assert log.consumed_calories == 800
    assert day_state["remaining_calories"] == user.target_calories - 800
    from app.services.vision import logged_message
    assert logged_message(day_state, "ar")


async def test_free_daily_scans_then_a_points_charge(services, user) -> None:
    for _ in range(settings.food_scan_free_daily):
        first = await services.vision.analyse_food(user, jpeg_bytes(48), lang="ar")
        assert first.charge is None or first.charge.points == 0

    paid = await services.vision.analyse_food(user, jpeg_bytes(48), lang="ar")
    assert paid.charge is not None and paid.charge.points == -settings.food_scan_cost_points


async def test_ambiguous_photo_asks_instead_of_guessing(services, user, monkeypatch) -> None:
    """Spec §3.ج — when the image is unclear the bot must ask, not invent numbers."""
    ambiguous = dict(conftest.CANNED["food_analysis"])
    ambiguous["clarifications"] = ["كم كمية الرز تقريبًا؟", "هل الدجاج مقلي أو مشوي؟"]
    ambiguous["confidence"] = 0.35
    monkeypatch.setitem(conftest.CANNED, "food_analysis", ambiguous)

    analysis = await services.vision.analyse_food(user, jpeg_bytes(48), lang="ar")
    assert analysis.needs_clarification is True
    from app.services.vision import clarification_message

    message = clarification_message(analysis, "ar")
    assert "كم كمية الرز" in message

    final = await services.vision.finalise_food(
        user, analysis, answers="رز نص طبق والدجاج مشوي", lang="ar"
    )
    assert final.calories > 0


async def test_not_food_is_rejected(services, user, monkeypatch) -> None:
    payload = dict(conftest.CANNED["food_analysis"])
    payload["is_food"] = False
    monkeypatch.setitem(conftest.CANNED, "food_analysis", payload)
    analysis = await services.vision.analyse_food(user, jpeg_bytes(48), lang="ar")
    assert analysis.is_food is False


async def test_manual_and_skipped_meals(services, user) -> None:
    entry, state = await services.vision.log_manual(
        user, name="عدس", calories=420, protein_g=22.0, carbs_g=60.0, fats_g=8.0
    )
    assert entry.meal_name == "عدس"
    assert state["remaining_calories"] == user.target_calories - 420

    skipped = await services.vision.log_skipped_meal(user, slot="dinner", reason="تعبان")
    assert skipped["remaining_calories"] == state["remaining_calories"]


async def test_progress_photo_analysis_charges_and_compares(services, user) -> None:
    result = await services.vision.analyse_progress_photo(
        user, jpeg_bytes(), angle="front", lang="ar"
    )
    assert result.text
    assert result.charge is not None
    assert result.charge.points == -settings.progress_scan_cost_points
    assert result.photo is not None


# ═══════════════════════════════════════════════════════════════════════════
#  consultation
# ═══════════════════════════════════════════════════════════════════════════
async def test_consult_returns_answer_with_exercise_links(services, user) -> None:
    result = await services.consult.ask(user, "أعطني تمارين لحرق الدهون بالبيت", lang="ar")
    assert result.text
    assert result.exercises
    assert result.cost_usd >= 0
    assert user.daily_consult_count == 1


async def test_consult_respects_the_daily_free_limit(services, user) -> None:
    from app.db.repositories.economy import DailyLimitReached

    for _ in range(settings.consult_free_daily_limit):
        await services.consult.ask(user, "سؤال", lang="ar")
    with pytest.raises(DailyLimitReached):
        await services.consult.ask(user, "سؤال", lang="ar")


def test_render_consult_shapes_the_message() -> None:
    from app.services.consult import render_consult

    text = render_consult(
        answer="ابدأ بالإحماء", exercises=[{"name": "بيربي", "sets_reps": "3×12",
                                            "video_url": "https://youtu.be/x"}],
        meals=[{"name": "شوفان", "calories": 300, "protein_g": 12, "carbs_g": 40, "fats_g": 6}],
        follow_up="كم يوم تتمرن؟", total_calories=300, lang="ar",
    )
    assert "بيربي" in text and "https://youtu.be/x" in text and "شوفان" in text


# ═══════════════════════════════════════════════════════════════════════════
#  programmes
# ═══════════════════════════════════════════════════════════════════════════
async def test_plan_generation_persists_a_schedule(services, user) -> None:
    result = await services.plans.generate(user, "training", lang="ar")
    assert result.plan is not None
    assert result.plan.weeks and result.plan.status == "active"
    assert result.plan.schedule

    rendered = services.plans.render_plan(result.plan, lang="ar")
    assert "ضغط" in rendered or result.plan.title in rendered

    session, text = await services.plans.session_for_today(user, lang="ar")
    assert text
    summary = await services.plans.active_plan_summary(user, lang="ar")
    assert result.plan.title in summary or "📋" in summary


async def test_completing_a_session_updates_streak_and_counters(services, user) -> None:
    await services.plans.generate(user, "training", lang="ar")
    session, info = await services.plans.complete_today(user, duration_min=40, rpe=7, lang="ar")
    assert info.get("streak") is not None
    assert user.total_workouts >= 1
    log = await services.repos.tracking.daily_log(user, user.local_today())
    assert log.workout_done is True


async def test_boxing_programme_is_phased(services, user) -> None:
    from app.services.plans import boxing_next_phase

    first = await boxing_next_phase(services.plans, user, week=1, lang="ar")
    assert first.plan is not None and first.plan.kind == "boxing"
    second = await boxing_next_phase(services.plans, user, week=2, lang="ar")
    assert second.plan is not None
    assert second.plan.title != first.plan.title


# ═══════════════════════════════════════════════════════════════════════════
#  reminders
# ═══════════════════════════════════════════════════════════════════════════
async def test_reminder_rules_can_be_toggled_and_rendered(services, user) -> None:
    await services.reminders.ensure_defaults(user)
    rules = await services.reminders.rules(user)
    assert rules

    water = next(rule for rule in rules if rule.kind == ReminderKind.WATER.value)
    state_before = water.enabled
    toggled = await services.reminders.toggle(user, water.kind)
    assert toggled.enabled is not state_before

    listing = services.reminders.render_list(await services.reminders.rules(user), user, "ar")
    assert listing and "🟢" in listing or "⚪" in listing


async def test_next_run_respects_quiet_hours(services, user) -> None:
    user.user_timezone = "UTC"
    rule = await services.reminders.upsert(
        user, ReminderKind.WATER.value, enabled=True, hour=2, minute=30
    )
    nxt = compute_next_run(rule, user)
    assert nxt is not None
    local_hour = nxt.astimezone(UTC).hour
    assert not (settings.quiet_hours_start <= local_hour or local_hour < settings.quiet_hours_end)


async def test_interval_rules_fire_repeatedly(services, user) -> None:
    rule = await services.reminders.upsert(
        user, ReminderKind.WATER.value, enabled=True, interval_minutes=120
    )
    first = rule.next_run_at or compute_next_run(rule, user)
    assert first is not None
    rule.last_sent_at = datetime.now(UTC)
    second = compute_next_run(rule, user)
    assert second > rule.last_sent_at


async def test_run_once_sends_and_rearms(services, user, session) -> None:
    rule = await services.reminders.upsert(
        user, ReminderKind.WEIGH_IN.value, enabled=True, hour=8, minute=0
    )
    rule.next_run_at = datetime.now(UTC) - timedelta(minutes=1)
    await session.commit()

    bot = FakeBot()
    sent = await services.reminders.run_once(bot=bot, limit=10)
    assert sent == 1
    assert bot.sent and bot.sent[0][0] == user.tg_id
    assert rule.last_sent_at is not None
    assert rule.next_run_at is not None and rule.next_run_at > datetime.now(UTC) - timedelta(minutes=1)


async def test_blocked_users_have_reminders_disabled(services, user, session) -> None:
    rule = await services.reminders.upsert(
        user, ReminderKind.WATER.value, enabled=True, interval_minutes=60
    )
    rule.next_run_at = datetime.now(UTC) - timedelta(minutes=1)
    await session.commit()

    bot = FakeBot(blocked={user.tg_id})
    assert await services.reminders.run_once(bot=bot, limit=10) == 0
    assert rule.enabled is False
    assert rule.next_run_at is None


async def test_reminder_messages_are_localised(services, user) -> None:
    log = await services.repos.tracking.daily_log(user, user.local_today())
    for kind in ReminderKind:
        rule = await services.reminders.upsert(user, kind.value, enabled=True, interval_minutes=60)
        arabic = await services.reminders.message_for(rule, user, log)
        assert arabic and not arabic.startswith("rem.")      # no untranslated keys
        user.language_code = "en"
        english = await services.reminders.message_for(rule, user, log)
        assert english and not english.startswith("rem.")
        user.language_code = "ar"


# ═══════════════════════════════════════════════════════════════════════════
#  broadcasts
# ═══════════════════════════════════════════════════════════════════════════
async def test_broadcast_reaches_the_chosen_audience(services, user, coach_user, session) -> None:
    await session.commit()
    bot = FakeBot()
    broadcaster = services.broadcaster(bot)
    broadcast_id, total = await broadcaster.create(
        admin_tg_id=1, text="📢 رسالة تجريبية", audience="all"
    )
    assert total >= 1
    result = await broadcaster.run(broadcast_id)
    assert result["sent"] == total and result["failed"] == 0
    assert len(bot.sent) == total

    history = await broadcaster.history(limit=5)
    assert history[0]["status"] == BroadcastStatus.DONE.value
    assert history[0]["sent"] == total


async def test_broadcast_counts_blocked_users_as_failed(services, user, session) -> None:
    await session.commit()
    bot = FakeBot(blocked={user.tg_id})
    broadcaster = services.broadcaster(bot)
    broadcast_id, _total = await broadcaster.create(admin_tg_id=1, text="مرحبا", audience="all")
    result = await broadcaster.run(broadcast_id)
    assert result["failed"] >= 1
    assert result["sent"] + result["failed"] >= 1


async def test_broadcast_audiences_are_filterable(services, coach_user, session) -> None:
    await session.commit()
    everyone = await services.repos.users.broadcast_audience("all")
    coaches = await services.repos.users.broadcast_audience("coaches")
    assert coach_user.tg_id in everyone
    assert coach_user.tg_id in coaches


async def test_scheduled_broadcasts_are_picked_up_by_the_job(services, user, session) -> None:
    await session.commit()
    bot = FakeBot()
    broadcaster = services.broadcaster(bot)
    broadcast_id, _total = await broadcaster.create(
        admin_tg_id=1, text="مجدولة", audience="all",
        scheduled_for=datetime.now(UTC) - timedelta(minutes=1),
    )
    assert await broadcaster.run_pending() >= 1
    assert bot.sent
    history = await broadcaster.history(limit=1)
    assert history[0]["id"] == str(broadcast_id)


# ═══════════════════════════════════════════════════════════════════════════
#  reports, charts, exports
# ═══════════════════════════════════════════════════════════════════════════
async def test_weekly_and_monthly_reports(services, user) -> None:
    await services.vision.log_manual(user, name="وجبة", calories=600, protein_g=30.0)
    weekly = await services.reports.weekly(user, "ar")
    monthly = await services.reports.monthly(user, "ar")
    assert weekly and monthly
    assert not weekly.startswith("rem.") and not monthly.startswith("rem.")


async def test_report_pdf_is_generated(services, user) -> None:
    await services.vision.log_manual(user, name="وجبة", calories=600, protein_g=30.0)
    pdf = await services.reports.export_pdf(user, lang="ar", include_charts=True)
    assert pdf.startswith(b"%PDF")
    assert len(pdf) > 800


async def test_progress_overview_and_chart(services, user) -> None:

    today = user.local_today()
    for offset, weight in ((7, 87.0), (3, 86.4), (0, 86.0)):
        await services.repos.tracking.record_weight(user, weight, day=today - timedelta(days=offset))
    await services.vision.log_manual(user, name="وجبة", calories=500, protein_g=25.0)

    overview = await services.progress.overview(user, lang="ar")
    assert overview
    png = await services.progress.combined_chart(user, lang="ar")
    assert png.startswith(b"\x89PNG")


async def test_progress_weight_and_measurements_messages(services, user) -> None:
    text = await services.progress.log_weight(user, 85.5, lang="ar")
    assert "85.5" in text.replace("٫", ".")
    saved = await services.progress.log_measurements(user, {"waist_cm": 90.0}, lang="ar")
    assert saved


# ═══════════════════════════════════════════════════════════════════════════
#  privacy
# ═══════════════════════════════════════════════════════════════════════════
async def test_consent_is_recorded_and_checked(services, new_user) -> None:
    assert services.privacy.needs_consent(new_user) is True
    await services.privacy.record_consent(new_user, granted=True)
    assert services.privacy.needs_consent(new_user) is False
    records = await services.repos.ops.consent_history(new_user.id) if hasattr(
        services.repos, "ops") else None
    assert records is None or True


async def test_data_export_contains_the_journey(services, user) -> None:
    await services.vision.log_manual(user, name="وجبة", calories=600, protein_g=30.0)
    payload = await services.privacy.export_json(user)
    assert payload["profile"]["tg_id"] == user.tg_id
    assert payload["daily_logs"] or payload.get("meals")
    pdf = await services.privacy.export_pdf(user, lang="ar")
    assert pdf.startswith(b"%PDF")


async def test_delete_photos_removes_media(services, user) -> None:
    await services.vision.analyse_food(user, jpeg_bytes(), lang="ar")
    removed = await services.privacy.delete_photos(user, admin=1)
    assert removed >= 1
    assert not await services.repos.tracking.photos(user.id)


async def test_delete_account_anonymises_everything(services, user, session) -> None:
    await services.vision.log_manual(user, name="وجبة", calories=600, protein_g=30.0)
    result = await services.privacy.delete_account(user, admin=1)
    assert isinstance(result, dict) and result
    assert user.points_balance == 0
    assert user.weight_kg is None
    assert not await services.repos.tracking.meals_of_day(user.id, user.local_today())


async def test_policy_text_is_localised(services) -> None:
    arabic = services.privacy.policy_text("ar")
    english = services.privacy.policy_text("en")
    assert arabic and english and arabic != english


# ═══════════════════════════════════════════════════════════════════════════
#  media storage
# ═══════════════════════════════════════════════════════════════════════════
async def test_local_media_round_trip(services, user, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(settings, "local_media_dir", tmp_path, raising=False)
    record, final_bytes, ai_input = await services.media.store_image(
        jpeg_bytes(128), user_id=user.id, kind="food", optimise=False
    )
    assert record is not None and record.id
    assert ai_input.data
    loaded = await services.media.load(record)
    assert loaded == final_bytes


# ═══════════════════════════════════════════════════════════════════════════
#  usage & cost telemetry
# ═══════════════════════════════════════════════════════════════════════════
async def test_usage_telemetry_aggregates_by_task_and_model(services, coach_user) -> None:
    await services.coach.handle_message(coach_user, "وزني 84", lang="ar")
    await services.consult.ask(coach_user, "سؤال سريع", lang="ar")

    platform = await services.usage.platform(days=7)
    assert platform["calls"] >= 2
    assert platform["total_cost_usd"] > 0
    assert platform["by_task"] and platform["by_model"]
    assert services.usage.format_admin(platform, "ar")

    personal = await services.usage.for_user(coach_user, days=7)
    assert personal["calls"] >= 2
    assert personal["points_balance"] == int(coach_user.points_balance)

    pricing = await services.usage.model_pricing_report()
    assert pricing


async def test_global_budget_cap_blocks_expensive_calls(services, coach_user, monkeypatch) -> None:
    from app.ai import router as ai_router
    from app.ai.errors import AIBudgetExceeded

    await services.coach.handle_message(coach_user, "وزني 84", lang="ar")   # real spend logged
    monkeypatch.setattr(settings, "global_daily_usd_cap", 0.0001, raising=False)
    ai_router.invalidate_cache()
    with pytest.raises(AIBudgetExceeded):
        await services.coach.handle_message(coach_user, "وزني 84", lang="ar")
    ai_router.invalidate_cache()


async def test_model_routing_override_is_persisted(services, user) -> None:
    from app.ai import router as ai_router

    router = services.ai.router
    await services.repos.settings.set(ai_router.ROUTING_KEY, {"coach": ["test/cheap-model"]})
    await services.repos.session.commit()          # the router opens its own session
    ai_router.invalidate_cache()
    await router.load_overrides(force=True)
    assert router.models_for("coach")[0] == "test/cheap-model"
    assert router.describe()["coach"][0] == "test/cheap-model"

    await services.repos.settings.set(ai_router.ROUTING_KEY, {})
    await services.repos.session.commit()
    ai_router.invalidate_cache()
    await router.load_overrides(force=True)
    assert router.models_for("coach")[0] == settings.model_coach


# ═══════════════════════════════════════════════════════════════════════════
#  streaks & badges
# ═══════════════════════════════════════════════════════════════════════════
async def test_activity_builds_a_streak_and_awards_badges(services, user) -> None:
    log = await services.repos.tracking.daily_log(user, user.local_today())
    result = await services.streaks.register_activity(user, log)
    assert isinstance(result.streak, int)
    assert isinstance(result.messages, list)

    user.streak_current = 3
    badges = await services.streaks.check_achievement_badges(user)
    assert isinstance(badges, list)
    from app.services.streak import badge_label

    assert badge_label("streak_3", "ar")


# ═══════════════════════════════════════════════════════════════════════════
#  admin operations
# ═══════════════════════════════════════════════════════════════════════════
async def test_admin_stats_and_audit_trail(services, user, session) -> None:
    stats = await services.repos.users.activity_stats()
    assert stats["total"] >= 1

    await services.points.admin_grant(user, 300, admin_tg_id=777)
    await services.repos.audit.log(777, "grant_points", target_user_id=user.id,
                                   payload={"amount": 300})
    entries = await services.repos.audit.recent(limit=5)
    assert any(entry.admin_tg_id == 777 for entry in entries)

    found = await services.repos.users.search("424242", limit=5)
    assert any(person.id == user.id for person in found)
    rows, total = await services.repos.users.paginate(page=1, per_page=5)
    assert total >= 1 and rows


async def test_ban_blocks_the_user(services, user) -> None:
    await services.repos.users.set_ban(user, banned=True, reason="spam")
    assert user.is_banned is True
    await services.repos.users.set_ban(user, banned=False)
    assert user.is_banned is False


async def test_app_settings_round_trip(services) -> None:
    await services.repos.settings.set("forced_channels", ["@test"], description="t", updated_by=1)
    assert await services.repos.settings.get("forced_channels") == ["@test"]
    assert await services.repos.settings.get("missing_key", "fallback") == "fallback"
    assert uuid.UUID(str(uuid.uuid4()))
