"""Economy data access: points ledger, daily/monthly caps, badges, referrals."""
from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.constants import POINTS_SPEND_REASONS, BadgeKey, PointsReason
from app.db.models import PointsLedgerEntry, ReferralEvent, User, UserBadge, as_aware, utcnow


class InsufficientPoints(Exception):
    """Raised when a spend would drive the balance below zero."""

    def __init__(self, required: int, available: int) -> None:
        self.required = required
        self.available = available
        super().__init__(f"need {required} points, have {available}")


class DailyLimitReached(Exception):
    def __init__(self, kind: str, limit: int) -> None:
        self.kind = kind
        self.limit = limit
        super().__init__(f"daily limit reached for {kind} ({limit})")


class EconomyRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.s = session

    # ── locking helper (FOR UPDATE is a no-op concept on SQLite) ─────────────
    def _lock(self, stmt: Any) -> Any:
        if settings.uses_postgres:
            return stmt.with_for_update()
        return stmt

    async def locked_user(self, user_id: uuid.UUID) -> User:
        stmt = self._lock(select(User).where(User.id == user_id))
        user = (await self.s.execute(stmt)).scalar_one_or_none()
        if user is None:
            raise LookupError(f"user {user_id} not found")
        return user

    # ── usage windows ────────────────────────────────────────────────────────
    async def ensure_windows(self, user: User) -> User:
        """Reset daily/monthly counters when the calendar rolls over."""
        today = user.local_today()
        if user.usage_day != today:
            user.usage_day = today
            user.daily_consult_count = 0
            user.daily_coach_messages = 0
            user.daily_vision_count = 0
            user.daily_food_scans_free_used = 0
            user.daily_points_spent = 0
        month_key = today.strftime("%Y-%m")
        if user.usage_month != month_key:
            user.usage_month = month_key
            user.monthly_points_spent = 0
        await self.s.flush()
        return user

    # ── points ───────────────────────────────────────────────────────────────
    async def change_points(
        self,
        user: User,
        amount: int,
        reason: PointsReason | str,
        *,
        note: str | None = None,
        reference: dict[str, Any] | None = None,
        actor_tg_id: int | None = None,
        allow_negative: bool = False,
    ) -> PointsLedgerEntry:
        """Append-only ledger write + balance update.

        ``allow_negative`` is reserved for admin corrections; normal spends raise
        :class:`InsufficientPoints` so the bot can ask the user to top up.
        """
        reason = PointsReason(reason) if not isinstance(reason, PointsReason) else reason
        amount = int(amount)
        if amount == 0:
            amount = 1 if reason in POINTS_SPEND_REASONS else 0
        if amount < 0 and not allow_negative and (user.points_balance + amount) < 0:
            raise InsufficientPoints(required=abs(amount), available=int(user.points_balance))

        new_balance = int(user.points_balance or 0) + amount
        user.points_balance = new_balance
        if amount > 0:
            user.total_points_earned = int(user.total_points_earned or 0) + amount
        else:
            spent = abs(amount)
            user.total_points_spent = int(user.total_points_spent or 0) + spent
            await self.ensure_windows(user)
            user.daily_points_spent = int(user.daily_points_spent or 0) + spent
            user.monthly_points_spent = int(user.monthly_points_spent or 0) + spent

        entry = PointsLedgerEntry(
            user_id=user.id,
            amount=amount,
            balance_after=new_balance,
            reason=reason.value,
            note=note,
            reference=reference or {},
            actor_tg_id=actor_tg_id,
        )
        self.s.add(entry)
        await self.s.flush()
        return entry

    async def ledger(
        self, user_id: uuid.UUID, limit: int = 20, offset: int = 0
    ) -> Sequence[PointsLedgerEntry]:
        return (
            await self.s.execute(
                select(PointsLedgerEntry)
                .where(PointsLedgerEntry.user_id == user_id)
                .order_by(PointsLedgerEntry.created_at.desc())
                .limit(limit)
                .offset(offset)
            )
        ).scalars().all()

    async def ledger_count(self, user_id: uuid.UUID) -> int:
        return int(
            (
                await self.s.execute(
                    select(func.count(PointsLedgerEntry.id)).where(
                        PointsLedgerEntry.user_id == user_id
                    )
                )
            ).scalar_one()
        )

    async def totals(self) -> dict[str, Any]:
        """Platform-wide point economy snapshot for the admin dashboard."""
        rows = (
            await self.s.execute(
                select(PointsLedgerEntry.reason, func.sum(PointsLedgerEntry.amount), func.count())
                .group_by(PointsLedgerEntry.reason)
            )
        ).all()
        return {r[0]: {"points": int(r[1] or 0), "count": int(r[2])} for r in rows}

    # ── caps ─────────────────────────────────────────────────────────────────
    async def check_limits(self, user: User, kind: str) -> None:
        """Raise :class:`DailyLimitReached` when a per-user cap is exhausted."""
        await self.ensure_windows(user)
        limits = {
            "consult": (int(user.daily_consult_count or 0), settings.consult_free_daily_limit),
            "coach": (int(user.daily_coach_messages or 0), settings.coach_daily_message_limit),
            "vision": (int(user.daily_vision_count or 0), settings.vision_daily_limit),
        }
        if kind in limits:
            used, cap = limits[kind]
            if cap and used >= cap:
                raise DailyLimitReached(kind, cap)
        if settings.user_daily_points_cap and int(user.daily_points_spent or 0) >= settings.user_daily_points_cap:
            raise DailyLimitReached("points_daily", settings.user_daily_points_cap)
        if settings.user_monthly_points_cap and int(user.monthly_points_spent or 0) >= settings.user_monthly_points_cap:
            raise DailyLimitReached("points_monthly", settings.user_monthly_points_cap)

    async def bump_counter(self, user: User, kind: str, by: int = 1) -> int:
        await self.ensure_windows(user)
        field = {
            "consult": "daily_consult_count",
            "coach": "daily_coach_messages",
            "vision": "daily_vision_count",
        }.get(kind)
        if not field:
            return 0
        value = int(getattr(user, field) or 0) + by
        setattr(user, field, value)
        await self.s.flush()
        return value

    async def use_free_food_scan(self, user: User) -> bool:
        """Consume one of the free daily food scans; True if a free one was used."""
        await self.ensure_windows(user)
        if int(user.daily_food_scans_free_used or 0) < settings.food_scan_free_daily:
            user.daily_food_scans_free_used = int(user.daily_food_scans_free_used or 0) + 1
            await self.s.flush()
            return True
        return False

    # ── coach access ─────────────────────────────────────────────────────────
    async def grant_coach_access(self, user: User, days: int | None = None) -> datetime | None:
        days = settings.coach_access_days if days is None else days
        if days <= 0:
            user.coach_unlocked_forever = True
            user.coach_access_until = None
        else:
            base = max(user.coach_access_until, utcnow()) if user.coach_access_until else utcnow()
            user.coach_access_until = base + timedelta(days=days)
        await self.s.flush()
        return user.coach_access_until

    async def set_subscription(self, user: User, plan: str, days: int) -> None:
        user.subscription_plan = plan
        if plan == "none" or days <= 0:
            user.subscription_expires_at = None
        else:
            base = utcnow()
            current = as_aware(user.subscription_expires_at)
            if current and current > base:
                base = user.subscription_expires_at
            user.subscription_expires_at = base + timedelta(days=days)
        await self.s.flush()

    async def due_subscriptions(self, now: datetime | None = None) -> Sequence[User]:
        now = now or utcnow()
        return (
            await self.s.execute(
                select(User).where(
                    User.subscription_plan != "none",
                    User.subscription_expires_at > now,
                    User.is_banned.is_(False),
                    (User.subscription_last_grant_at.is_(None))
                    | (User.subscription_last_grant_at <= now - timedelta(days=29)),
                )
            )
        ).scalars().all()

    # ── badges ───────────────────────────────────────────────────────────────
    async def has_badge(self, user_id: uuid.UUID, key: BadgeKey | str) -> bool:
        key = key.value if isinstance(key, BadgeKey) else key
        return (
            await self.s.execute(
                select(func.count(UserBadge.id)).where(
                    UserBadge.user_id == user_id, UserBadge.badge_key == key
                )
            )
        ).scalar_one() > 0

    async def award_badge(
        self, user_id: uuid.UUID, key: BadgeKey | str, meta: dict[str, Any] | None = None
    ) -> UserBadge | None:
        key = key.value if isinstance(key, BadgeKey) else key
        if await self.has_badge(user_id, key):
            return None
        badge = UserBadge(user_id=user_id, badge_key=key, earned_at=utcnow(), meta=meta or {})
        self.s.add(badge)
        await self.s.flush()
        return badge

    async def badges_of(self, user_id: uuid.UUID) -> Sequence[UserBadge]:
        return (
            await self.s.execute(
                select(UserBadge).where(UserBadge.user_id == user_id).order_by(UserBadge.earned_at)
            )
        ).scalars().all()

    # ── referrals ────────────────────────────────────────────────────────────
    async def referral_event(self, invitee_id: uuid.UUID) -> ReferralEvent | None:
        return (
            await self.s.execute(select(ReferralEvent).where(ReferralEvent.invitee_id == invitee_id))
        ).scalar_one_or_none()

    async def record_referral(
        self,
        inviter: User,
        invitee: User,
        inviter_points: int,
        invitee_points: int,
        qualified: bool = False,
    ) -> ReferralEvent:
        event = ReferralEvent(
            inviter_id=inviter.id,
            invitee_id=invitee.id,
            code=inviter.referral_code,
            inviter_points=inviter_points,
            invitee_points=invitee_points,
            qualified=qualified,
        )
        self.s.add(event)
        await self.s.flush()
        return event

    async def referral_stats(self, inviter_id: uuid.UUID) -> dict[str, Any]:
        rows = (
            await self.s.execute(
                select(func.count(ReferralEvent.id), func.sum(ReferralEvent.inviter_points)).where(
                    ReferralEvent.inviter_id == inviter_id
                )
            )
        ).one()
        return {"count": int(rows[0] or 0), "points_earned": int(rows[1] or 0)}

    async def mark_referral_qualified(self, invitee_id: uuid.UUID) -> ReferralEvent | None:
        event = await self.referral_event(invitee_id)
        if event and not event.qualified:
            event.qualified = True
            await self.s.flush()
        return event


def day_start(dt: datetime | None = None) -> datetime:
    dt = dt or utcnow()
    return dt.replace(hour=0, minute=0, second=0, microsecond=0)


def month_start(dt: datetime | None = None) -> date:
    dt = dt or utcnow()
    return dt.replace(day=1).date()
