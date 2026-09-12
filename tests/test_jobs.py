"""Background-job tests: the scheduler layer that runs reminders, broadcasts,
subscription renewals, weekly reports and privacy purges in production.

A bug here is invisible in the chat UI — the feature simply never happens — so
each job is executed for real against the test database with a fake bot.
"""
from __future__ import annotations

from datetime import timedelta

import pytest_asyncio

from app.config import settings
from app.constants import ReminderKind, SubscriptionPlan
from app.db.models import MediaFile, as_aware, utcnow
from app.jobs import scheduler as jobs
from app.state import referral_base_link, set_bot
from app.state import state as runtime
from tests import conftest
from tests.test_integration import FakeBot


# ═══════════════════════════════════════════════════════════════════════════
#  fixtures
# ═══════════════════════════════════════════════════════════════════════════
@pytest_asyncio.fixture
async def bot(session) -> FakeBot:
    """Install a fake bot as the process-wide runtime bot the jobs read."""
    fake = FakeBot()
    previous = runtime.bot
    runtime.bot = fake                                    # type: ignore[assignment]
    yield fake
    runtime.bot = previous                                # type: ignore[assignment]


@pytest_asyncio.fixture(autouse=True)
async def _no_real_scheduler():
    """Never leave a live APScheduler loop behind after a test.

    This has to be an *async* fixture: AsyncIOScheduler.shutdown() posts a callback
    onto the loop it was started on, so tearing it down from a sync fixture (after
    pytest-asyncio closed that loop) raises "Event loop is closed".
    """
    yield
    if jobs.scheduler is not None:
        if jobs.scheduler.running:
            jobs.scheduler.shutdown(wait=False)
        jobs.scheduler = None
        runtime.scheduler = None


async def _reload(session, obj):
    await session.refresh(obj)
    return obj


# ═══════════════════════════════════════════════════════════════════════════
#  lifecycle
# ═══════════════════════════════════════════════════════════════════════════
async def test_scheduler_registers_every_job() -> None:
    scheduler = jobs.start_scheduler()
    ids = {job.id for job in scheduler.get_jobs()}
    assert ids == {
        "reminders", "broadcasts", "renewals", "weekly-reports",
        "media-purge", "inactive-purge", "streak-reset",
    }
    assert scheduler.running is True
    assert runtime.scheduler is scheduler


async def test_start_scheduler_is_idempotent() -> None:
    first = jobs.start_scheduler()
    second = jobs.start_scheduler()
    assert first is second
    assert len(first.get_jobs()) == 7                     # jobs were not duplicated


async def test_job_triggers_match_the_documented_cadence() -> None:
    from apscheduler.triggers.cron import CronTrigger
    from apscheduler.triggers.interval import IntervalTrigger

    scheduler = jobs.start_scheduler()
    by_id = {job.id: job for job in scheduler.get_jobs()}

    assert isinstance(by_id["reminders"].trigger, IntervalTrigger)
    assert isinstance(by_id["broadcasts"].trigger, IntervalTrigger)
    for job_id in ("renewals", "weekly-reports", "media-purge", "inactive-purge", "streak-reset"):
        assert isinstance(by_id[job_id].trigger, CronTrigger), job_id

    # coalescing + single instance: a missed tick must not fire a burst
    for job in by_id.values():
        assert job.max_instances == 1
        assert job.coalesce is True


async def test_shutdown_scheduler_clears_the_runtime() -> None:
    jobs.start_scheduler()
    await jobs.shutdown_scheduler()
    assert jobs.scheduler is None
    assert runtime.scheduler is None
    # a second shutdown is a no-op, not an error
    await jobs.shutdown_scheduler()


def test_job_guard_swallows_exceptions() -> None:
    """One bad tick must never kill the loop."""

    @jobs._guard("boom")
    async def failing() -> int:
        raise RuntimeError("simulated job failure")

    import asyncio

    assert asyncio.run(failing()) is None


def test_referral_base_link_follows_the_bot_username() -> None:
    set_bot(FakeBot(), username="ai_coach_bot")            # type: ignore[arg-type]
    assert referral_base_link() == "https://t.me/ai_coach_bot"
    runtime.bot_username = None
    assert referral_base_link().startswith("https://t.me/")


# ═══════════════════════════════════════════════════════════════════════════
#  reminders job (extra §1)
# ═══════════════════════════════════════════════════════════════════════════
async def test_reminders_job_delivers_and_rearms(services, user, session, bot, monkeypatch) -> None:
    monkeypatch.setattr(settings, "reminders_enabled", True, raising=False)
    rule = await services.reminders.upsert(user, ReminderKind.WATER.value, enabled=True,
                                           interval_minutes=90)
    rule.next_run_at = utcnow() - timedelta(minutes=5)      # due now
    await session.commit()

    sent = await jobs.reminders_job()
    assert sent >= 1
    assert bot.sent, "the reminder never reached the user"
    assert any(user.tg_id == chat_id for chat_id, _ in bot.sent)

    await _reload(session, rule)
    assert rule.next_run_at is not None
    assert as_aware(rule.next_run_at) > utcnow()             # re-armed, no double-fire
    assert rule.last_sent_at is not None


async def test_reminders_job_skips_users_who_blocked_the_bot(services, user, session,
                                                             monkeypatch) -> None:
    monkeypatch.setattr(settings, "reminders_enabled", True, raising=False)
    blocked = FakeBot(blocked={user.tg_id})
    runtime.bot = blocked                                   # type: ignore[assignment]
    rule = await services.reminders.upsert(user, ReminderKind.WATER.value, enabled=True,
                                           interval_minutes=90)
    rule.next_run_at = utcnow() - timedelta(minutes=5)
    await session.commit()

    await jobs.reminders_job()                              # must not raise
    await _reload(session, rule)
    assert rule.enabled is False                            # disabled so we stop retrying


async def test_reminders_job_is_a_noop_without_a_bot(session, monkeypatch) -> None:
    monkeypatch.setattr(settings, "reminders_enabled", True, raising=False)
    runtime.bot = None
    assert await jobs.reminders_job() == 0


async def test_reminders_job_respects_the_feature_flag(services, user, session, bot,
                                                       monkeypatch) -> None:
    monkeypatch.setattr(settings, "reminders_enabled", False, raising=False)
    rule = await services.reminders.upsert(user, ReminderKind.WATER.value, enabled=True,
                                           interval_minutes=90)
    rule.next_run_at = utcnow() - timedelta(minutes=5)
    await session.commit()

    assert await jobs.reminders_job() == 0
    assert not bot.sent


# ═══════════════════════════════════════════════════════════════════════════
#  broadcast job (spec §7)
# ═══════════════════════════════════════════════════════════════════════════
async def test_broadcast_job_delivers_pending_broadcasts(services, user, session, bot) -> None:
    await session.commit()
    broadcast_id, total = await services.broadcaster(bot).create(     # type: ignore[arg-type]
        admin_tg_id=1, text="📢 رسالة من المجدول", audience="all"
    )
    await session.commit()

    delivered = await jobs.broadcast_job()
    assert delivered >= 1
    assert any(text == "📢 رسالة من المجدول" for _, text in bot.sent)

    history = await services.broadcaster(bot).history(limit=1)         # type: ignore[arg-type]
    assert history[0]["id"] == str(broadcast_id)
    assert history[0]["sent"] == total


async def test_broadcast_job_ignores_future_scheduled_runs(services, user, session, bot) -> None:
    await session.commit()
    await services.broadcaster(bot).create(                            # type: ignore[arg-type]
        admin_tg_id=1, text="لاحقًا", audience="all",
        scheduled_for=utcnow() + timedelta(hours=2),
    )
    await session.commit()

    assert await jobs.broadcast_job() == 0
    assert not bot.sent


async def test_broadcast_job_is_a_noop_without_a_bot(session) -> None:
    runtime.bot = None
    assert await jobs.broadcast_job() == 0


# ═══════════════════════════════════════════════════════════════════════════
#  subscription renewals (extra §10)
# ═══════════════════════════════════════════════════════════════════════════
async def test_renewals_job_pays_a_due_subscriber(services, user, session, bot) -> None:
    await services.subscription.activate(user, SubscriptionPlan.MONTHLY.value)
    before = user.points_balance
    user.subscription_last_grant_at = utcnow() - timedelta(days=31)    # cycle came due
    user.subscription_expires_at = utcnow() + timedelta(days=5)
    await session.commit()

    renewed = await jobs.renewals_job()
    assert renewed == 1

    await _reload(session, user)
    assert user.points_balance > before
    assert as_aware(user.subscription_last_grant_at) > utcnow() - timedelta(minutes=5)

    ledger = await services.repos.economy.ledger(user.id, limit=10)
    assert any(entry.reason == "subscription_renew" for entry in ledger)


async def test_renewals_job_skips_subscribers_not_yet_due(services, user, session, bot) -> None:
    await services.subscription.activate(user, SubscriptionPlan.MONTHLY.value)
    before = user.points_balance
    await session.commit()

    assert await jobs.renewals_job() == 0                   # granted moments ago
    await _reload(session, user)
    assert user.points_balance == before


async def test_renewals_job_skips_banned_subscribers(services, user, session, bot) -> None:
    await services.subscription.activate(user, SubscriptionPlan.MONTHLY.value)
    user.subscription_last_grant_at = utcnow() - timedelta(days=31)
    user.is_banned = True
    await session.commit()

    assert await jobs.renewals_job() == 0


async def test_renewals_job_extends_an_expiring_subscription(services, user, session, bot) -> None:
    """A renewal must never leave an active subscriber expired."""
    await services.subscription.activate(user, SubscriptionPlan.MONTHLY.value)
    user.subscription_last_grant_at = utcnow() - timedelta(days=31)
    user.subscription_expires_at = utcnow() + timedelta(hours=6)       # expires today
    await session.commit()

    assert await jobs.renewals_job() == 1
    await _reload(session, user)
    assert as_aware(user.subscription_expires_at) > utcnow() + timedelta(days=1)


# ═══════════════════════════════════════════════════════════════════════════
#  weekly reports (spec §3.ب)
# ═══════════════════════════════════════════════════════════════════════════
async def test_weekly_reports_job_pushes_to_coach_subscribers(services, coach_user, session,
                                                              bot) -> None:
    await session.commit()
    await jobs.weekly_reports_job()
    assert bot.sent, "no weekly report was delivered to the coach subscriber"
    assert any(coach_user.tg_id == chat_id for chat_id, _ in bot.sent)


async def test_weekly_reports_job_skips_users_without_coach_access(services, user, session,
                                                                   bot) -> None:
    await session.commit()
    await jobs.weekly_reports_job()
    assert not any(chat_id == user.tg_id for chat_id, _ in bot.sent)


async def test_weekly_reports_job_is_a_noop_without_a_bot(session) -> None:
    runtime.bot = None
    await jobs.weekly_reports_job()                         # must not raise


# ═══════════════════════════════════════════════════════════════════════════
#  privacy / maintenance jobs (extra §14, GDPR)
# ═══════════════════════════════════════════════════════════════════════════
async def test_media_purge_job_removes_expired_files(services, user, session, bot) -> None:
    expired = MediaFile(user_id=user.id, kind="food", backend="local",
                        storage_key="media/expired.jpg",
                        expires_at=utcnow() - timedelta(days=1))
    fresh = MediaFile(user_id=user.id, kind="food", backend="local",
                      storage_key="media/fresh.jpg",
                      expires_at=utcnow() + timedelta(days=30))
    session.add_all([expired, fresh])
    await session.commit()

    purged = await jobs.media_purge_job()
    assert purged >= 1

    await _reload(session, expired)
    await _reload(session, fresh)
    assert expired.purged_at is not None
    assert fresh.purged_at is None


async def test_inactive_purge_job_anonymises_dormant_accounts(services, session, bot) -> None:
    dormant = conftest.make_user(tg_id=31337, username="ghost", first_name="Ghost",
                                 referral_code="GHOST01",
                                 last_active_at=utcnow() - timedelta(days=500))
    active = conftest.make_user(tg_id=31338, username="alive", first_name="Alive",
                                referral_code="ALIVE01", last_active_at=utcnow())
    session.add_all([dormant, active])
    await session.commit()

    purged = await jobs.inactive_purge_job()
    assert purged >= 1

    await _reload(session, dormant)
    await _reload(session, active)
    assert dormant.data_deleted_at is not None
    assert active.data_deleted_at is None


async def test_streak_check_job_reschedules_reminders(services, user, session, bot) -> None:
    rule = await services.reminders.upsert(user, ReminderKind.WEIGH_IN.value, enabled=True,
                                           hour=7, minute=0)
    rule.next_run_at = None                                  # lost schedule
    await session.commit()

    await jobs.streak_check_job()
    await _reload(session, rule)
    assert rule.next_run_at is not None


# ═══════════════════════════════════════════════════════════════════════════
#  robustness
# ═══════════════════════════════════════════════════════════════════════════
async def test_jobs_survive_a_broken_database(session, bot, monkeypatch) -> None:
    """A job that blows up must return quietly rather than take the loop with it."""
    from app.services.reminders import ReminderService

    async def explode(self, *args, **kwargs):
        raise RuntimeError("database on fire")

    monkeypatch.setattr(ReminderService, "run_once", explode)
    monkeypatch.setattr(settings, "reminders_enabled", True, raising=False)

    assert await jobs.reminders_job() is None


async def test_every_job_is_callable_without_arguments() -> None:
    """APScheduler invokes jobs with no arguments — signatures must allow that."""
    import inspect

    for name in ("reminders_job", "broadcast_job", "renewals_job", "weekly_reports_job",
                 "media_purge_job", "inactive_purge_job", "streak_check_job"):
        job = getattr(jobs, name)
        params = inspect.signature(job).parameters
        variadic = (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD)
        required = [
            p for p in params.values()
            if p.default is inspect.Parameter.empty and p.kind not in variadic
        ]
        assert not required, f"{name} requires arguments: {required}"
