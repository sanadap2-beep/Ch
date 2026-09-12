"""Reminder system (spec §5.1) — water, workout, weekly weigh-in, meal log.

One APScheduler tick per minute asks :meth:`ReminderService.run_once`, which
selects rows whose ``next_run_at`` has passed (indexed), sends them, and
recomputes the next run in **the user's own timezone**, honouring quiet hours.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.config import settings
from app.constants import ReminderKind
from app.db.models import DailyLog, ReminderRule, User, as_aware, utcnow
from app.db.repositories import Repos
from app.i18n import t

logger = logging.getLogger(__name__)

KIND_LABELS_AR = {
    ReminderKind.WATER.value: "💧 شرب الماء",
    ReminderKind.WORKOUT.value: "🏋️ موعد التمرين",
    ReminderKind.WEIGH_IN.value: "⚖️ تسجيل الوزن الأسبوعي",
    ReminderKind.MEAL_LOG.value: "🍽️ تسجيل الوجبات",
    ReminderKind.PROGRESS_PHOTO.value: "📷 صورة تقدم شهرية",
    ReminderKind.COACH_CHECKIN.value: "🤝 متابعة الكوتش اليومية",
}
KIND_LABELS_EN = {
    ReminderKind.WATER.value: "💧 Drink water",
    ReminderKind.WORKOUT.value: "🏋️ Workout time",
    ReminderKind.WEIGH_IN.value: "⚖️ Weekly weigh-in",
    ReminderKind.MEAL_LOG.value: "🍽️ Log your meals",
    ReminderKind.PROGRESS_PHOTO.value: "📷 Monthly progress photo",
    ReminderKind.COACH_CHECKIN.value: "🤝 Daily coach check-in",
}


def kind_label(kind: str, lang: str = "ar") -> str:
    table = KIND_LABELS_AR if lang.startswith("ar") else KIND_LABELS_EN
    return table.get(kind, kind)


def _tz(user: User) -> Any:
    try:
        return ZoneInfo(user.user_timezone or settings.timezone or "UTC")
    except Exception:  # noqa: BLE001
        return ZoneInfo("UTC")


def compute_next_run(
    rule: ReminderRule, user: User, *, now: datetime | None = None
) -> datetime | None:
    """Next UTC instant this rule should fire (quiet-hours aware)."""
    if not rule.enabled:
        return None
    now = now or utcnow()
    tz = _tz(user)
    local_now = now.astimezone(tz)

    if rule.interval_minutes:
        base = rule.last_sent_at.astimezone(tz) if rule.last_sent_at else local_now
        candidate = base + timedelta(minutes=int(rule.interval_minutes))
        if candidate <= local_now:
            candidate = local_now + timedelta(minutes=1)
    elif rule.hour is not None:
        candidate = local_now.replace(hour=int(rule.hour), minute=int(rule.minute or 0),
                                      second=0, microsecond=0)
        days = [int(d) for d in (rule.days_of_week or [])]
        if days:
            for _ in range(8):
                if candidate.weekday() in days and candidate > local_now:
                    break
                candidate += timedelta(days=1)
                candidate = candidate.replace(hour=int(rule.hour), minute=int(rule.minute or 0))
        elif candidate <= local_now:
            candidate += timedelta(days=1)
    else:
        return None

    # quiet hours → push to the end of the quiet window
    start, end = settings.quiet_hours_start, settings.quiet_hours_end
    hour = candidate.hour
    in_quiet = (hour >= start or hour < end) if start > end else (start <= hour < end)
    if in_quiet:
        candidate = candidate.replace(hour=end % 24, minute=0, second=0, microsecond=0)
        if candidate <= local_now:
            candidate += timedelta(days=1)

    return candidate.astimezone(ZoneInfo("UTC"))


class ReminderService:
    def __init__(self, repos: Repos, bot: Bot | None = None) -> None:
        self.repos = repos
        self.bot = bot

    # ── configuration ────────────────────────────────────────────────────────
    async def rules(self, user: User) -> list[ReminderRule]:
        rows = (
            await self.repos.session.execute(
                select(ReminderRule).where(ReminderRule.user_id == user.id).order_by(ReminderRule.kind)
            )
        ).scalars().all()
        return list(rows)

    async def rule(self, user: User, kind: str) -> ReminderRule | None:
        row = (
            await self.repos.session.execute(
                select(ReminderRule).where(ReminderRule.user_id == user.id, ReminderRule.kind == kind)
            )
        ).scalar_one_or_none()
        return row

    async def upsert(
        self,
        user: User,
        kind: str,
        *,
        enabled: bool | None = None,
        hour: int | None = None,
        minute: int | None = None,
        days_of_week: list[int] | None = None,
        interval_minutes: int | None = None,
    ) -> ReminderRule:
        rule = await self.rule(user, kind)
        if rule is None:
            rule = ReminderRule(user_id=user.id, kind=kind)
            self.repos.session.add(rule)
        if enabled is not None:
            rule.enabled = bool(enabled)
        if hour is not None:
            rule.hour = max(0, min(23, int(hour)))
            rule.interval_minutes = None
        if minute is not None:
            rule.minute = max(0, min(59, int(minute)))
        if days_of_week is not None:
            rule.days_of_week = [int(d) % 7 for d in days_of_week]
        if interval_minutes is not None:
            rule.interval_minutes = max(10, int(interval_minutes))
            rule.hour = None
        rule.next_run_at = compute_next_run(rule, user)
        await self.repos.session.flush()
        return rule

    async def toggle(self, user: User, kind: str) -> ReminderRule:
        rule = await self.rule(user, kind)
        return await self.upsert(user, kind, enabled=not (rule.enabled if rule else False))

    async def reschedule_all(self, user: User) -> int:
        count = 0
        for rule in await self.rules(user):
            rule.next_run_at = compute_next_run(rule, user)
            count += 1
        await self.repos.session.flush()
        return count

    def render_list(self, rules: list[ReminderRule], user: User, lang: str = "ar") -> str:
        if not rules:
            return "—" if not lang.startswith("ar") else "لا توجد تذكيرات مفعّلة"
        lines = []
        for rule in rules:
            state = "🟢" if rule.enabled else "⚪️"
            when = ""
            if rule.interval_minutes:
                when = f"كل {rule.interval_minutes} دقيقة" if lang.startswith("ar") else f"every {rule.interval_minutes} min"
            elif rule.hour is not None:
                when = f"{rule.hour:02d}:{rule.minute or 0:02d}"
                if rule.days_of_week:
                    names = ["الاثنين", "الثلاثاء", "الأربعاء", "الخميس", "الجمعة", "السبت", "الأحد"]
                    if lang.startswith("ar"):
                        when += " — " + "، ".join(names[d % 7] for d in sorted(rule.days_of_week))
                    else:
                        when += " — " + ", ".join(["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"][d % 7]
                                                  for d in sorted(rule.days_of_week))
            lines.append(f"{state} {kind_label(rule.kind, lang)}: {when}")
        return "\n".join(lines)

    # ── dispatch ─────────────────────────────────────────────────────────────
    async def due_rules(self, *, now: datetime | None = None, limit: int = 300) -> list[ReminderRule]:
        now = now or utcnow()
        rows = (
            await self.repos.session.execute(
                select(ReminderRule)
                .options(selectinload(ReminderRule.user))
                .where(
                    ReminderRule.enabled.is_(True),
                    ReminderRule.next_run_at.is_not(None),
                    ReminderRule.next_run_at <= now,
                )
                .limit(limit)
            )
        ).scalars().all()
        return [rule for rule in rows if rule.user is not None and not rule.user.is_banned]

    async def message_for(self, rule: ReminderRule, user: User, log: DailyLog | None = None) -> str:
        lang = user.language_code or "ar"
        kind = rule.kind
        if kind == ReminderKind.WATER.value:
            target = int(log.water_target_ml or user.water_target_ml or 2500) if log else int(user.water_target_ml or 2500)
            done = int(log.water_ml or 0) if log else 0
            return t(lang, "rem.water_msg", target=target, done=done, left=max(0, target - done))
        if kind == ReminderKind.WORKOUT.value:
            title = "—"
            try:
                plan = await self.repos.plans.any_active_plan(user.id)
                if plan and plan.schedule:
                    today = user.local_today()
                    slot = plan.schedule[today.weekday() % len(plan.schedule)]
                    title = str(slot.get("name") or slot.get("focus") or "—")
            except Exception:  # noqa: BLE001 — a reminder must never crash
                logger.debug("workout reminder: plan lookup failed", exc_info=True)
            return t(lang, "rem.workout_msg", title=title, days=int(user.streak_current or 0))
        if kind == ReminderKind.WEIGH_IN.value:
            return t(lang, "rem.weigh_msg")
        if kind == ReminderKind.MEAL_LOG.value:
            left = int(log.remaining_calories) if log else int(user.target_calories or 0)
            protein = float(log.remaining_macros["protein_g"]) if log else float(user.target_protein_g or 0)
            return t(lang, "rem.meal_msg", left=left, protein=protein)
        if kind == ReminderKind.PROGRESS_PHOTO.value:
            photos = await self.repos.tracking.photos(user.id, limit=1)
            days_since = 30
            if photos:
                taken_at = as_aware(photos[0].taken_at) or utcnow()
                days_since = max(0, (utcnow() - taken_at).days)
            return t(lang, "rem.photo_msg", days=days_since)
        if kind == ReminderKind.COACH_CHECKIN.value:
            return t(lang, "rem.checkin_msg")
        return t(lang, "rem.checkin_msg")

    async def send(self, rule: ReminderRule, *, bot: Bot | None = None) -> bool:
        bot = bot or self.bot
        if bot is None:
            logger.warning("reminder skipped: no bot instance")
            return False
        user = rule.user
        try:
            log = await self.repos.tracking.daily_log(user, user.local_today())
            text = await self.message_for(rule, user, log)
            await bot.send_message(user.tg_id, text)
            rule.last_sent_at = utcnow()
            rule.next_run_at = compute_next_run(rule, user)
            await self.repos.session.flush()
            return True
        except TelegramForbiddenError:
            logger.info("user %s blocked the bot — disabling reminder", user.tg_id)
            rule.enabled = False
            rule.next_run_at = None
            await self.repos.session.flush()
            return False
        except TelegramAPIError as exc:
            logger.warning("reminder send failed for %s: %s", user.tg_id, exc)
            # push forward so we don't hammer a failing chat
            rule.next_run_at = utcnow() + timedelta(minutes=30)
            await self.repos.session.flush()
            return False

    async def run_once(self, *, bot: Bot | None = None, limit: int = 300) -> int:
        due = await self.due_rules(limit=limit)
        sent = 0
        for rule in due:
            if await self.send(rule, bot=bot):
                sent += 1
        return sent

    async def ensure_defaults(self, user: User) -> int:
        """Seed reminders for users created before the feature existed."""
        existing = {rule.kind for rule in await self.rules(user)}
        created = 0
        for kind in (ReminderKind.WATER.value, ReminderKind.WEIGH_IN.value):
            if kind in existing:
                continue
            await self.upsert(
                user,
                kind,
                enabled=True,
                hour=None if kind == ReminderKind.WATER.value else settings.default_weigh_in_hour,
                interval_minutes=(
                    settings.water_reminder_interval_min if kind == ReminderKind.WATER.value else None
                ),
                days_of_week=None if kind == ReminderKind.WATER.value else [settings.default_weigh_in_day],
            )
            created += 1
        return created
