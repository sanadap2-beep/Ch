"""Streaks, badges and reward loops (spec §5.2)."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from app.config import settings
from app.constants import BadgeKey, PointsReason
from app.db.models import DailyLog, User
from app.db.repositories import Repos

logger = logging.getLogger(__name__)

BADGE_LABELS_AR: dict[str, str] = {
    BadgeKey.PROFILE_COMPLETE.value: "🎯 ملف مكتمل",
    BadgeKey.STREAK_3.value: "🔥 3 أيام التزام",
    BadgeKey.STREAK_7.value: "🔥 أسبوع كامل",
    BadgeKey.STREAK_30.value: "💎 شهر من الالتزام",
    BadgeKey.STREAK_100.value: "👑 100 يوم",
    BadgeKey.FIRST_WORKOUT.value: "💪 أول تمرين",
    BadgeKey.FIRST_MEAL_LOGGED.value: "🍽️ أول وجبة مسجلة",
    BadgeKey.WEIGHT_LOSS_5KG.value: "⚖️ خسرت 5 كغ",
    BadgeKey.WEIGHT_LOSS_10KG.value: "⚖️ خسرت 10 كغ",
    BadgeKey.WEIGHT_GAIN_5KG.value: "📈 زدت 5 كغ",
    BadgeKey.PHOTO_MONTH.value: "📷 شهر من التوثيق",
    BadgeKey.REFERRED_5.value: "🎁 5 دعوات ناجحة",
    BadgeKey.BOXING_PHASE_1.value: "🥊 أنهيت المرحلة الأولى",
}
BADGE_LABELS_EN: dict[str, str] = {
    BadgeKey.PROFILE_COMPLETE.value: "🎯 Profile complete",
    BadgeKey.STREAK_3.value: "🔥 3-day streak",
    BadgeKey.STREAK_7.value: "🔥 Full week",
    BadgeKey.STREAK_30.value: "💎 30-day streak",
    BadgeKey.STREAK_100.value: "👑 100 days",
    BadgeKey.FIRST_WORKOUT.value: "💪 First workout",
    BadgeKey.FIRST_MEAL_LOGGED.value: "🍽️ First meal logged",
    BadgeKey.WEIGHT_LOSS_5KG.value: "⚖️ Lost 5 kg",
    BadgeKey.WEIGHT_LOSS_10KG.value: "⚖️ Lost 10 kg",
    BadgeKey.WEIGHT_GAIN_5KG.value: "📈 Gained 5 kg",
    BadgeKey.PHOTO_MONTH.value: "📷 A month of photos",
    BadgeKey.REFERRED_5.value: "🎁 5 referrals",
    BadgeKey.BOXING_PHASE_1.value: "🥊 Phase 1 complete",
}


def badge_label(key: str, lang: str = "ar") -> str:
    table = BADGE_LABELS_AR if lang.startswith("ar") else BADGE_LABELS_EN
    return table.get(key, key)


@dataclass(slots=True)
class StreakResult:
    streak: int = 0
    increased: bool = False
    new_badges: list[str] = field(default_factory=list)
    points_awarded: int = 0
    messages: list[str] = field(default_factory=list)


class StreakService:
    def __init__(self, repos: Repos) -> None:
        self.repos = repos

    async def register_activity(self, user: User, log: DailyLog | None = None) -> StreakResult:
        """Call after any meaningful action (meal logged, workout done, weigh-in)."""
        today = user.local_today()
        result = StreakResult()
        yesterday = today - timedelta(days=1)

        if user.last_activity_day == today:
            result.streak = int(user.streak_current or 0)
            return result

        if user.last_activity_day == yesterday:
            user.streak_current = int(user.streak_current or 0) + 1
            result.increased = True
        elif user.last_activity_day is None:
            user.streak_current = 1
            result.increased = True
        else:
            # gap > 1 day → streak resets, but we keep the best
            user.streak_current = 1
            result.increased = True
            result.messages.append("reset")
        user.last_activity_day = today
        user.streak_best = max(int(user.streak_best or 0), int(user.streak_current or 0))
        result.streak = int(user.streak_current or 0)
        await self.repos.session.flush()

        # milestone badges + points
        for milestone in settings.streak_milestones:
            if result.streak == milestone:
                key = {3: BadgeKey.STREAK_3, 7: BadgeKey.STREAK_7, 30: BadgeKey.STREAK_30,
                       100: BadgeKey.STREAK_100}.get(milestone)
                if key:
                    badge = await self.repos.economy.award_badge(
                        user.id, key, {"streak": milestone}
                    )
                    if badge:
                        result.new_badges.append(key.value)
                        if settings.streak_reward_points:
                            await self.repos.economy.change_points(
                                user,
                                settings.streak_reward_points,
                                PointsReason.STREAK_REWARD,
                                note=f"مكافأة التزام {milestone} يوم",
                                reference={"streak": milestone},
                            )
                            result.points_awarded += settings.streak_reward_points
        return result

    async def check_achievement_badges(self, user: User, extra: dict[str, Any] | None = None) -> list[str]:
        """Weight-loss / first-workout / photo badges — checked after updates."""
        extra = extra or {}
        earned: list[str] = []
        first_weight = extra.get("first_weight") or await self._first_weight(user)
        current = user.weight_kg
        if first_weight and current:
            delta = float(first_weight) - float(current)
            if delta >= 5:
                if await self.repos.economy.award_badge(user.id, BadgeKey.WEIGHT_LOSS_5KG, {"delta": round(delta, 1)}):
                    earned.append(BadgeKey.WEIGHT_LOSS_5KG.value)
            if delta >= 10:
                if await self.repos.economy.award_badge(user.id, BadgeKey.WEIGHT_LOSS_10KG, {"delta": round(delta, 1)}):
                    earned.append(BadgeKey.WEIGHT_LOSS_10KG.value)
            if delta <= -5:
                if await self.repos.economy.award_badge(user.id, BadgeKey.WEIGHT_GAIN_5KG, {"delta": round(delta, 1)}):
                    earned.append(BadgeKey.WEIGHT_GAIN_5KG.value)
        if int(user.total_workouts or 0) >= 1:
            if await self.repos.economy.award_badge(user.id, BadgeKey.FIRST_WORKOUT):
                earned.append(BadgeKey.FIRST_WORKOUT.value)
        if int(user.total_meals_logged or 0) >= 1:
            if await self.repos.economy.award_badge(user.id, BadgeKey.FIRST_MEAL_LOGGED):
                earned.append(BadgeKey.FIRST_MEAL_LOGGED.value)
        if int(user.referrals_count or 0) >= 5:
            if await self.repos.economy.award_badge(user.id, BadgeKey.REFERRED_5):
                earned.append(BadgeKey.REFERRED_5.value)
        return earned

    async def _first_weight(self, user: User) -> float | None:
        from sqlalchemy import select

        from app.db.models import WeightRecord

        row = (
            await self.repos.session.execute(
                select(WeightRecord.weight_kg)
                .where(WeightRecord.user_id == user.id)
                .order_by(WeightRecord.recorded_on)
                .limit(1)
            )
        ).scalar_one_or_none()
        return float(row) if row else None

    async def leaderboard(self, limit: int = 10) -> list[dict[str, Any]]:
        from sqlalchemy import select

        rows = (
            await self.repos.session.execute(
                select(User).where(User.is_banned.is_(False)).order_by(User.streak_current.desc()).limit(limit)
            )
        ).scalars().all()
        return [
            {"name": u.full_name, "streak": int(u.streak_current or 0), "points": int(u.points_balance or 0)}
            for u in rows
        ]

    def summary(self, user: User, lang: str = "ar") -> str:
        if lang.startswith("ar"):
            return (
                f"🔥 الالتزام الحالي: <b>{user.streak_current or 0}</b> يوم "
                f"(الأفضل: {user.streak_best or 0})\n"
                f"💪 تمارين مكتملة: {user.total_workouts or 0} | 🍽️ وجبات مسجلة: {user.total_meals_logged or 0}"
            )
        return (
            f"🔥 Current streak: <b>{user.streak_current or 0}</b> days (best: {user.streak_best or 0})\n"
            f"💪 Workouts: {user.total_workouts or 0} | 🍽️ Meals logged: {user.total_meals_logged or 0}"
        )

    async def photo_badge_if_due(self, user: User) -> list[str]:
        photos = await self.repos.tracking.photos(user.id, limit=200)
        if len(photos) < 4:
            return []
        span = photos[0].taken_at - photos[-1].taken_at
        if span >= timedelta(days=28):
            badge = await self.repos.economy.award_badge(user.id, BadgeKey.PHOTO_MONTH, {"photos": len(photos)})
            return [BadgeKey.PHOTO_MONTH.value] if badge else []
        return []


def days_until_next_milestone(streak: int) -> tuple[int, int] | None:
    for milestone in sorted(settings.streak_milestones):
        if streak < milestone:
            return milestone - streak, milestone
    return None
