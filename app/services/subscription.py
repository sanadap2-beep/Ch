"""Monthly/quarterly subscriptions with automatic point renewal (spec §5.9).

Top-ups are currently **admin-managed** (per the chosen payment model): an admin
activates a plan for a user, and the scheduler renews the points automatically
every cycle until expiry.  The interface is deliberately payment-agnostic so a
Telegram Stars / payment-provider implementation can be plugged in later by
adding one ``activate()`` caller.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from app.config import settings
from app.constants import AdminAction, SubscriptionPlan
from app.db.models import User, as_aware, utcnow
from app.db.repositories import Repos
from app.services.points import ChargeResult, PointsService

logger = logging.getLogger(__name__)

PLAN_DAYS = {
    SubscriptionPlan.MONTHLY.value: 30,
    SubscriptionPlan.QUARTERLY.value: 90,
}
PLAN_POINTS = {
    SubscriptionPlan.MONTHLY.value: settings.monthly_plan_points,
    SubscriptionPlan.QUARTERLY.value: settings.quarterly_plan_points,
}


@dataclass(slots=True)
class RenewalResult:
    user_id: Any
    tg_id: int
    points: int
    ok: bool
    error: str | None = None


class SubscriptionService:
    def __init__(self, repos: Repos, points: PointsService | None = None) -> None:
        self.repos = repos
        self.points = points or PointsService(repos.economy)

    # ── lifecycle ────────────────────────────────────────────────────────────
    async def activate(
        self,
        user: User,
        plan: str,
        *,
        admin_tg_id: int | None = None,
        days: int | None = None,
        grant_now: bool = True,
    ) -> ChargeResult | None:
        plan = (plan or "none").lower()
        days = PLAN_DAYS.get(plan, days or 30) if plan != "none" else 0
        await self.repos.economy.set_subscription(user, plan, days)
        if admin_tg_id is not None:
            await self.repos.audit.log(
                admin_tg_id, AdminAction.SET_SUBSCRIPTION, target_user_id=user.id,
                payload={"plan": plan, "days": days},
            )
        if grant_now and plan != "none":
            result = await self.points.subscription_grant(user, plan=plan, admin_tg_id=admin_tg_id)
            user.subscription_last_grant_at = utcnow()
            await self.repos.session.flush()
            return result
        return None

    async def cancel(self, user: User, *, admin_tg_id: int | None = None) -> None:
        await self.repos.economy.set_subscription(user, "none", 0)
        if admin_tg_id is not None:
            await self.repos.audit.log(
                admin_tg_id, AdminAction.SET_SUBSCRIPTION, target_user_id=user.id,
                payload={"plan": "none"},
            )

    # ── scheduler entry point ────────────────────────────────────────────────
    async def run_renewals(self) -> list[RenewalResult]:
        """Grant points to every active subscriber whose cycle came due."""
        due = await self.repos.economy.due_subscriptions()
        results: list[RenewalResult] = []
        for user in due:
            plan = user.subscription_plan
            points = PLAN_POINTS.get(plan, settings.monthly_plan_points)
            try:
                await self.repos.economy.change_points(
                    user,
                    points,
                    "subscription_renew",
                    note=f"تجديد تلقائي — {plan}",
                    reference={"plan": plan},
                )
                user.subscription_last_grant_at = utcnow()
                # extend expiry so a renewal never leaves an active user expired
                expires = as_aware(user.subscription_expires_at)
                if expires and expires < utcnow() + timedelta(days=1):
                    user.subscription_expires_at = user.subscription_expires_at + timedelta(days=PLAN_DAYS.get(plan, 30))
                results.append(RenewalResult(user.id, user.tg_id, points, True))
            except Exception as exc:  # noqa: BLE001 — one bad user must not stop the batch
                logger.exception("subscription renewal failed for %s", user.tg_id)
                results.append(RenewalResult(user.id, user.tg_id, 0, False, str(exc)))
        await self.repos.session.flush()
        return results

    # ── display ──────────────────────────────────────────────────────────────
    def status_text(self, user: User, lang: str = "ar") -> str:
        ar = lang.startswith("ar")
        if user.subscription_plan == "none" or not user.has_subscription:
            return (
                "لا يوجد اشتراك نشط.\n"
                f"الاشتراك الشهري يضيف <b>{settings.monthly_plan_points} نقطة</b> تلقائيًا كل 30 يومًا.\n"
                "التفعيل يتم عبر الإدارة (شحن يدوي حاليًا)."
                if ar
                else "No active subscription.\n"
                f"The monthly plan adds <b>{settings.monthly_plan_points} points</b> automatically every 30 days.\n"
                "Activation is handled by the admin (manual top-up model)."
            )
        until = user.subscription_expires_at.strftime("%Y-%m-%d") if user.subscription_expires_at else "—"
        points = PLAN_POINTS.get(user.subscription_plan, settings.monthly_plan_points)
        return (
            f"✅ اشتراك <b>{user.subscription_plan}</b> نشط حتى {until}\n"
            f"نقاط الدورة القادمة: +{points}\n"
            f"رصيدك الحالي: <b>{user.points_balance}</b>"
            if ar
            else f"✅ <b>{user.subscription_plan}</b> active until {until}\n"
            f"Next cycle: +{points} points\nCurrent balance: <b>{user.points_balance}</b>"
        )
