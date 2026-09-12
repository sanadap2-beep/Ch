"""APScheduler jobs: reminders, broadcasts, subscription renewals, maintenance.

Every job opens its own session and swallows its own errors, so one bad tick can
never stop the next one.  All jobs are idempotent:

* reminders only fire when ``next_run_at`` has passed and are re-armed at once;
* broadcasts advance ``sent``/``failed`` as they go and are marked done once;
* renewals only pay users whose ``subscription_expires_at`` is due.
"""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app.config import settings
from app.db.base import session_scope
from app.services.context import Services
from app.state import state as runtime

logger = logging.getLogger(__name__)

scheduler: AsyncIOScheduler | None = None

Job = Callable[..., Awaitable[Any]]


def _guard(name: str) -> Callable[[Job], Job]:
    """Wrap a job so exceptions are logged instead of killing the scheduler."""

    def decorator(func: Job) -> Job:
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            try:
                return await func(*args, **kwargs)
            except Exception:  # noqa: BLE001 — a failing job must never stop the loop
                logger.exception("job %s failed", name)
                return None

        wrapper.__name__ = func.__name__
        return wrapper

    return decorator


# ═══════════════════════════════════════════════════════════════════════════
#  jobs
# ═══════════════════════════════════════════════════════════════════════════
@_guard("reminders")
async def reminders_job() -> int:
    """Spec extra §1 — water / workout / weigh-in / meal / photo / check-in."""
    if not settings.reminders_enabled or runtime.bot is None:
        return 0
    async with session_scope() as session:
        services = Services(session, runtime.bot)
        try:
            return await services.reminders.run_once(bot=runtime.bot, limit=300)
        finally:
            await services.close()


@_guard("broadcasts")
async def broadcast_job() -> int:
    """Spec §7 — deliver pending/scheduled broadcasts in rate-limited batches."""
    if runtime.bot is None:
        return 0
    async with session_scope() as session:
        services = Services(session, runtime.bot)
        try:
            return await services.broadcaster(runtime.bot).run_pending()
        finally:
            await services.close()


@_guard("subscription-renewals")
async def renewals_job() -> int:
    """Spec extra §10 — auto-renewing monthly/quarterly point grants."""
    async with session_scope() as session:
        services = Services(session, runtime.bot)
        try:
            results = await services.subscription.run_renewals()
        finally:
            await services.close()
    paid = [item for item in results if item.ok]
    if paid:
        logger.info("renewed %d subscriptions (%d points)", len(paid), sum(i.points for i in paid))
    return len(paid)


@_guard("weekly-reports")
async def weekly_reports_job() -> None:
    """Spec §3.ب — weekly report pushed to every active coach subscriber."""
    if runtime.bot is None:
        return
    from app.utils.tg import notify

    async with session_scope() as session:
        services = Services(session, runtime.bot)
        try:
            tg_ids = await services.repos.users.broadcast_audience("coaches")
            sent = 0
            for tg_id in tg_ids[:400]:
                person = await services.repos.users.by_tg_id(tg_id)
                if person is None or not person.has_coach_access:
                    continue
                try:
                    text = await services.reports.weekly(person, person.language_code or "ar")
                except Exception:  # noqa: BLE001 — one bad report must not stop the batch
                    logger.warning("weekly report failed for %s", tg_id, exc_info=True)
                    continue
                if await notify(runtime.bot, tg_id, text):
                    sent += 1
            logger.info("weekly reports sent: %d", sent)
        finally:
            await services.close()


@_guard("media-purge")
async def media_purge_job() -> int:
    """Spec extra §14 — privacy: purge media past its retention window."""
    async with session_scope() as session:
        services = Services(session, runtime.bot)
        try:
            purged = await services.privacy.purge_expired_media()
            purged += await services.media.purge_expired(limit=400)
        finally:
            await services.close()
    if purged:
        logger.info("purged %d media files", purged)
    return purged


@_guard("inactive-purge")
async def inactive_purge_job() -> int:
    """GDPR data minimisation: anonymise accounts inactive for a long time."""
    async with session_scope() as session:
        services = Services(session, runtime.bot)
        try:
            return await services.repos.users.purge_inactive(inactive_for_days=400)
        finally:
            await services.close()


@_guard("streak-reset")
async def streak_check_job() -> None:
    """Re-arm reminder schedules after midnight in every user timezone."""
    async with session_scope() as session:
        services = Services(session, runtime.bot)
        try:
            people, _total = await services.repos.users.paginate(page=1, per_page=500, onboarded=True)
            for person in people:
                await services.reminders.reschedule_all(person)
        finally:
            await services.close()


# ═══════════════════════════════════════════════════════════════════════════
#  lifecycle
# ═══════════════════════════════════════════════════════════════════════════
def start_scheduler() -> AsyncIOScheduler:
    """Register every job and start the loop (call once from the app lifespan)."""
    global scheduler
    if scheduler is not None:
        return scheduler

    scheduler = AsyncIOScheduler(timezone=settings.timezone, job_defaults={
        "coalesce": True,          # a missed run fires once, not N times
        "max_instances": 1,        # never overlap the same job
        "misfire_grace_time": 300,
    })

    scheduler.add_job(reminders_job, IntervalTrigger(seconds=60), id="reminders")
    scheduler.add_job(broadcast_job, IntervalTrigger(seconds=90), id="broadcasts")
    scheduler.add_job(renewals_job, CronTrigger(hour=3, minute=30), id="renewals")
    scheduler.add_job(
        weekly_reports_job,
        CronTrigger(day_of_week=settings.report_weekday, hour=settings.report_hour, minute=0),
        id="weekly-reports",
    )
    scheduler.add_job(media_purge_job, CronTrigger(hour=4, minute=15), id="media-purge")
    scheduler.add_job(inactive_purge_job, CronTrigger(day_of_week="mon", hour=5, minute=0),
                      id="inactive-purge")
    scheduler.add_job(streak_check_job, CronTrigger(hour=0, minute=10), id="streak-reset")

    try:
        scheduler.start()
    except RuntimeError as exc:
        # AsyncIOScheduler binds to the running loop — it must be started from the
        # async app lifespan, not from module import time or a sync entrypoint.
        scheduler = None
        raise RuntimeError(
            "start_scheduler() must be called from a running asyncio loop "
            "(e.g. inside the app lifespan in app/main.py)."
        ) from exc
    runtime.scheduler = scheduler
    logger.info("scheduler started with %d jobs", len(scheduler.get_jobs()))
    return scheduler


async def shutdown_scheduler() -> None:
    global scheduler
    if scheduler is None:
        return
    scheduler.shutdown(wait=False)
    logger.info("scheduler stopped")
    scheduler = None
    runtime.scheduler = None


__all__ = [
    "scheduler",
    "start_scheduler",
    "shutdown_scheduler",
    "reminders_job",
    "broadcast_job",
    "renewals_job",
    "weekly_reports_job",
    "media_purge_job",
    "inactive_purge_job",
    "streak_check_job",
]
