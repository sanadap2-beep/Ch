"""User aggregate data access."""
from __future__ import annotations

import secrets
import string
import uuid
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import Select, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import User, utcnow


def _referral_code() -> str:
    alphabet = string.ascii_uppercase.replace("O", "").replace("I", "") + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(7))


class UserRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.s = session

    # ── reads ────────────────────────────────────────────────────────────────
    async def by_tg_id(self, tg_id: int) -> User | None:
        return (await self.s.execute(select(User).where(User.tg_id == tg_id))).scalar_one_or_none()

    async def by_id(self, user_id: uuid.UUID | str) -> User | None:
        if isinstance(user_id, str):
            user_id = uuid.UUID(user_id)
        return await self.s.get(User, user_id)

    async def by_referral_code(self, code: str) -> User | None:
        code = (code or "").strip().upper().lstrip("@#")
        if not code:
            return None
        return (
            await self.s.execute(select(User).where(User.referral_code == code))
        ).scalar_one_or_none()

    async def search(self, query: str, limit: int = 20) -> Sequence[User]:
        q = query.strip()
        if q.lstrip("-").isdigit():
            tg = int(q)
            stmt = select(User).where(or_(User.tg_id == tg))
        else:
            like = f"%{q}%"
            stmt = select(User).where(
                or_(User.username.ilike(like), User.first_name.ilike(like), User.display_name.ilike(like))
            )
        return (await self.s.execute(stmt.limit(limit).order_by(User.created_at.desc()))).scalars().all()

    async def count_all(self) -> int:
        return int((await self.s.execute(select(func.count(User.id)))).scalar_one())

    async def count_active_since(self, since: datetime) -> int:
        return int(
            (
                await self.s.execute(
                    select(func.count(User.id)).where(User.last_active_at >= since)
                )
            ).scalar_one()
        )

    async def count_banned(self) -> int:
        return int(
            (await self.s.execute(select(func.count(User.id)).where(User.is_banned.is_(True)))).scalar_one()
        )

    async def activity_stats(self) -> dict[str, int]:
        now = utcnow()
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        week_start = today_start - timedelta(days=7)
        month_start = today_start - timedelta(days=30)
        return {
            "total": await self.count_all(),
            "today": await self.count_active_since(today_start),
            "week": await self.count_active_since(week_start),
            "month": await self.count_active_since(month_start),
            "banned": await self.count_banned(),
            "onboarded": int(
                (
                    await self.s.execute(
                        select(func.count(User.id)).where(User.onboarding_completed_at.is_not(None))
                    )
                ).scalar_one()
            ),
            "coaches": int(
                (
                    await self.s.execute(
                        select(func.count(User.id)).where(
                            or_(
                                User.coach_unlocked_forever.is_(True),
                                User.coach_access_until > now,
                            )
                        )
                    )
                ).scalar_one()
            ),
        }

    async def paginate(
        self,
        page: int = 1,
        per_page: int = 10,
        *,
        banned: bool | None = None,
        goal: str | None = None,
        onboarded: bool | None = None,
        order_by: str = "created_at",
    ) -> tuple[Sequence[User], int]:
        stmt: Select[Any] = select(User)
        count_stmt = select(func.count(User.id))
        if banned is not None:
            stmt = stmt.where(User.is_banned.is_(banned))
            count_stmt = count_stmt.where(User.is_banned.is_(banned))
        if goal:
            stmt = stmt.where(User.goal == goal)
            count_stmt = count_stmt.where(User.goal == goal)
        if onboarded is True:
            stmt = stmt.where(User.onboarding_completed_at.is_not(None))
            count_stmt = count_stmt.where(User.onboarding_completed_at.is_not(None))
        elif onboarded is False:
            stmt = stmt.where(User.onboarding_completed_at.is_(None))
            count_stmt = count_stmt.where(User.onboarding_completed_at.is_(None))

        col = {
            "created_at": User.created_at,
            "last_active_at": User.last_active_at,
            "points": User.points_balance,
            "streak": User.streak_current,
        }.get(order_by, User.created_at)
        total = int((await self.s.execute(count_stmt)).scalar_one())
        rows = (
            await self.s.execute(stmt.order_by(col.desc()).offset((page - 1) * per_page).limit(per_page))
        ).scalars().all()
        return rows, total

    async def broadcast_audience(self, audience: str = "all") -> list[int]:
        stmt = select(User.tg_id).where(User.is_banned.is_(False))
        now = utcnow()
        if audience == "active":
            stmt = stmt.where(User.last_active_at >= now - timedelta(days=7))
        elif audience == "coaches":
            stmt = stmt.where(
                or_(User.coach_unlocked_forever.is_(True), User.coach_access_until > now)
            )
        elif audience == "onboarded":
            stmt = stmt.where(User.onboarding_completed_at.is_not(None))
        return [int(r) for r in (await self.s.execute(stmt)).scalars().all()]

    # ── writes ───────────────────────────────────────────────────────────────
    async def get_or_create(
        self, tg_id: int, defaults: dict[str, Any] | None = None
    ) -> tuple[User, bool]:
        user = await self.by_tg_id(tg_id)
        if user is not None:
            return user, False
        payload: dict[str, Any] = {"tg_id": tg_id, **(defaults or {})}
        for _attempt in range(6):
            payload.setdefault("referral_code", _referral_code())
            user = User(**payload)
            self.s.add(user)
            try:
                await self.s.flush()
                return user, True
            except Exception:  # noqa: BLE001 — unique race on referral_code
                await self.s.rollback()
                payload.pop("referral_code", None)
                existing = await self.by_tg_id(tg_id)
                if existing is not None:
                    return existing, False
        raise RuntimeError("could not create user after retries")

    async def sync_telegram_profile(self, user: User, data: dict[str, Any]) -> bool:
        """Keep username/names in sync with Telegram. Returns True if changed."""
        changed = False
        # A language the user picked in ⚙️ Settings wins over their Telegram client
        # locale — otherwise the choice would be reverted on the very next message.
        locked = bool((user.preferences or {}).get("language_locked"))
        for field in ("username", "first_name", "last_name", "language_code"):
            if field == "language_code" and locked:
                continue
            new = data.get(field)
            if new is not None and getattr(user, field) != new:
                setattr(user, field, new)
                changed = True
        return changed

    async def apply(self, user: User, **fields: Any) -> User:
        for key, value in fields.items():
            if hasattr(user, key):
                setattr(user, key, value)
        await self.s.flush()
        return user

    async def touch(self, user: User) -> None:
        user.last_active_at = utcnow()
        await self.s.flush()

    async def set_ban(self, user: User, banned: bool, reason: str | None = None) -> None:
        user.is_banned = banned
        user.ban_reason = reason if banned else None
        user.banned_at = utcnow() if banned else None
        await self.s.flush()

    async def complete_onboarding(self, user: User) -> None:
        user.onboarding_completed_at = utcnow()
        user.onboarding_stage = None
        await self.s.flush()

    async def unique_referral_code(self) -> str:
        for _ in range(10):
            code = _referral_code()
            if await self.by_referral_code(code) is None:
                return code
        return _referral_code() + str(secrets.randbelow(9))

    async def increment_referrals(self, inviter: User) -> int:
        inviter.referrals_count = (inviter.referrals_count or 0) + 1
        await self.s.flush()
        return inviter.referrals_count

    async def purge_inactive(self, inactive_for_days: int = 400) -> int:
        cutoff = utcnow() - timedelta(days=inactive_for_days)
        res = await self.s.execute(
            update(User)
            .where(User.last_active_at < cutoff, User.onboarding_completed_at.is_(None))
            .values(data_deleted_at=utcnow())
        )
        return int(res.rowcount or 0)

    async def birthdays_of_today(self) -> Sequence[User]:  # placeholder hook for future CRM
        return []

    @staticmethod
    def local_date(user: User) -> date:
        """Current calendar date in the user's own timezone (falls back to UTC)."""
        try:
            from zoneinfo import ZoneInfo

            tz = ZoneInfo(user.user_timezone or "UTC")
        except Exception:  # noqa: BLE001
            tz = UTC
        return datetime.now(tz).date()
