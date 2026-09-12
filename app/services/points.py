"""Points economy — metered charging, ledger writes and balance checks.

Spec §7: *"يُربط استهلاك التوكنز الفعلي بنظام النقاط (تكلفة حسب طول الرد
الفعلي) بدل نقاط ثابتة، لتفادي خسارة مالية عند الردود الطويلة."*

So the personal coach is billed from the **real** token usage the provider
echoes back, clamped between a minimum and a maximum so a runaway reply can
never drain a user's balance in one message.  Free/cheap features stay flat.
"""
from __future__ import annotations

import logging
import math
import uuid
from dataclasses import dataclass
from typing import Any

from app.ai.client import Usage
from app.config import settings
from app.constants import BadgeKey, PointsReason, SubscriptionPlan
from app.db.models import User
from app.db.repositories.economy import (
    DailyLimitReached,
    EconomyRepository,
    InsufficientPoints,
)

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ChargeResult:
    points: int
    reason: str
    balance: int
    note: str | None = None
    entry_id: uuid.UUID | None = None

    @property
    def charged(self) -> bool:
        return self.points != 0


# ═══════════════════════════════════════════════════════════════════════════
#  pricing
# ═══════════════════════════════════════════════════════════════════════════
def points_from_usage(usage: Usage | None) -> int:
    """Token-metered price for one personal-coach turn."""
    if usage is None:
        return settings.coach_points_minimum
    value = (
        (usage.prompt_tokens / 1000.0) * settings.coach_points_per_1k_input
        + (usage.completion_tokens / 1000.0) * settings.coach_points_per_1k_output
    )
    return int(max(settings.coach_points_minimum, min(settings.coach_points_maximum, math.ceil(value))))


def points_from_usd(cost_usd: float) -> int:
    """Generic USD → points conversion (used by admin views and future billing)."""
    if cost_usd <= 0:
        return 0
    return max(1, int(math.ceil(cost_usd * settings.points_per_usd)))


def task_price(task: str) -> int:
    """Flat advertised price of a paid action (what the UI shows *before* asking)."""
    return {
        "coach_message": settings.coach_points_minimum,
        "vision_food": settings.food_scan_cost_points,
        "vision_progress": settings.progress_scan_cost_points,
        "coach_access": settings.coach_access_cost_points,
        "report_export": settings.data_export_cost_points,
    }.get(task, 0)


def balance_after_charge(user: User, points: int) -> int:
    return int(user.points_balance or 0) - int(points)


# ═══════════════════════════════════════════════════════════════════════════
#  service
# ═══════════════════════════════════════════════════════════════════════════
class PointsService:
    def __init__(self, economy: EconomyRepository) -> None:
        self.economy = economy

    # ── spends ───────────────────────────────────────────────────────────────
    async def charge_ai_turn(
        self,
        user: User,
        usage: Usage | None,
        *,
        reason: PointsReason = PointsReason.COACH_MESSAGE,
        model: str | None = None,
        cost_usd: float | None = None,
        note: str | None = None,
    ) -> ChargeResult:
        points = points_from_usage(usage)
        reference: dict[str, Any] = {
            "model": model,
            "cost_usd": round(float(cost_usd or 0.0), 6),
            "tokens": usage.as_dict() if usage else None,
        }
        entry = await self.economy.change_points(
            user, -points, reason, note=note, reference=reference
        )
        return ChargeResult(points=-points, reason=reason.value, balance=entry.balance_after,
                            note=note, entry_id=entry.id)

    async def charge_flat(
        self,
        user: User,
        points: int,
        reason: PointsReason,
        *,
        note: str | None = None,
        reference: dict[str, Any] | None = None,
    ) -> ChargeResult:
        if points <= 0:
            return ChargeResult(0, reason.value, int(user.points_balance or 0), note)
        entry = await self.economy.change_points(
            user, -points, reason, note=note, reference=reference or {}
        )
        return ChargeResult(-points, reason.value, entry.balance_after, note, entry.id)

    async def refund(self, user: User, points: int, note: str, original: dict[str, Any] | None = None) -> ChargeResult:
        """Give points back when an AI call failed after charging."""
        if points <= 0:
            return ChargeResult(0, PointsReason.REFUND.value, int(user.points_balance or 0))
        entry = await self.economy.change_points(
            user, points, PointsReason.REFUND, note=note, reference=original or {}
        )
        return ChargeResult(points, PointsReason.REFUND.value, entry.balance_after, note, entry.id)

    # ── credits ──────────────────────────────────────────────────────────────
    async def welcome_bonus(self, user: User) -> ChargeResult | None:
        if settings.welcome_bonus_points <= 0:
            return None
        existing = await self.economy.ledger(user.id, limit=50)
        if any(e.reason == PointsReason.WELCOME_BONUS.value for e in existing):
            return None
        entry = await self.economy.change_points(
            user, settings.welcome_bonus_points, PointsReason.WELCOME_BONUS,
            note="مكافأة الترحيب عند إنشاء الحساب",
        )
        return ChargeResult(entry.amount, entry.reason, entry.balance_after, entry.note, entry.id)

    async def admin_grant(
        self, user: User, points: int, *, admin_tg_id: int, note: str | None = None
    ) -> ChargeResult:
        entry = await self.economy.change_points(
            user, abs(int(points)), PointsReason.ADMIN_GRANT,
            note=note or "إضافة يدوية من الإدارة", actor_tg_id=admin_tg_id,
        )
        return ChargeResult(entry.amount, entry.reason, entry.balance_after, entry.note, entry.id)

    async def admin_deduct(
        self, user: User, points: int, *, admin_tg_id: int, note: str | None = None
    ) -> ChargeResult:
        entry = await self.economy.change_points(
            user, -abs(int(points)), PointsReason.ADMIN_DEDUCT,
            note=note or "خصم يدوي من الإدارة", actor_tg_id=admin_tg_id, allow_negative=True,
        )
        return ChargeResult(entry.amount, entry.reason, entry.balance_after, entry.note, entry.id)

    async def subscription_grant(
        self, user: User, *, plan: str | None = None, admin_tg_id: int | None = None
    ) -> ChargeResult:
        plan = plan or user.subscription_plan
        points = (
            settings.monthly_plan_points
            if plan == SubscriptionPlan.MONTHLY
            else settings.quarterly_plan_points
            if plan == SubscriptionPlan.QUARTERLY
            else 0
        )
        entry = await self.economy.change_points(
            user, points, PointsReason.SUBSCRIPTION_RENEW,
            note=f"تجديد اشتراك ({plan})", actor_tg_id=admin_tg_id,
            reference={"plan": plan},
        )
        return ChargeResult(entry.amount, entry.reason, entry.balance_after, entry.note, entry.id)

    # ── coach access ─────────────────────────────────────────────────────────
    async def unlock_coach(self, user: User) -> ChargeResult:
        cost = settings.coach_access_cost_points
        if cost > 0:
            await self.economy.change_points(
                user, -cost, PointsReason.COACH_ACCESS,
                note=f"فتح الكوتش الشخصي ({settings.coach_access_days} يوم)",
                reference={"days": settings.coach_access_days},
            )
        until = await self.economy.grant_coach_access(user)
        return ChargeResult(-cost, PointsReason.COACH_ACCESS.value, int(user.points_balance or 0),
                            note=until.isoformat() if until else "permanent")

    def can_unlock_coach(self, user: User) -> bool:
        return user.has_coach_access or int(user.points_balance or 0) >= settings.coach_access_cost_points

    # ── food scans ───────────────────────────────────────────────────────────
    async def charge_food_scan(self, user: User) -> ChargeResult:
        """First N scans of the day are free, then a small flat fee."""
        if await self.economy.use_free_food_scan(user):
            return ChargeResult(0, PointsReason.VISION_FOOD.value, int(user.points_balance or 0),
                                note="free daily scan")
        return await self.charge_flat(
            user, settings.food_scan_cost_points, PointsReason.VISION_FOOD,
            note="تحليل صورة وجبة",
        )

    async def charge_progress_scan(self, user: User) -> ChargeResult:
        return await self.charge_flat(
            user, settings.progress_scan_cost_points, PointsReason.VISION_PROGRESS,
            note="تحليل صورة تقدم الجسم",
        )

    # ── limits ───────────────────────────────────────────────────────────────
    async def ensure_can_use(self, user: User, kind: str, cost_points: int = 0) -> None:
        """Combined cap + balance check used before any AI call."""
        await self.economy.check_limits(user, kind)
        if cost_points > int(user.points_balance or 0):
            raise InsufficientPoints(required=cost_points, available=int(user.points_balance or 0))

    async def bump(self, user: User, kind: str) -> int:
        return await self.economy.bump_counter(user, kind)


__all__ = [
    "PointsService",
    "ChargeResult",
    "InsufficientPoints",
    "DailyLimitReached",
    "points_from_usage",
    "points_from_usd",
    "task_price",
    "balance_after_charge",
    "BadgeKey",
]
