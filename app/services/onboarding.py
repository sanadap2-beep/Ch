"""Onboarding completion: compute the plan, screen safety, grant bonuses.

Called once by the FSM when the last question is answered (and again whenever
the user edits their profile), so the numbers always stay consistent with the
stored profile.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select

from app.constants import BadgeKey
from app.db.models import User
from app.db.repositories import Repos
from app.services.metabolism import NutritionTargets, apply_targets, compute_targets, format_targets_ar
from app.services.points import ChargeResult, PointsService
from app.services.referral import ReferralOutcome, ReferralService
from app.services.safety import SafetyScreen, SafetyService
from app.services.streak import StreakService

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class OnboardingResult:
    user: User
    targets: NutritionTargets
    safety: SafetyScreen | None = None
    welcome: ChargeResult | None = None
    referral: ReferralOutcome | None = None
    badges: list[str] = field(default_factory=list)
    reminders_created: int = 0
    messages: dict[str, Any] = field(default_factory=dict)

    @property
    def plan_text_ar(self) -> str:
        return format_targets_ar(self.user, self.targets)


class OnboardingService:
    def __init__(
        self,
        repos: Repos,
        *,
        safety: SafetyService | None = None,
        points: PointsService | None = None,
        referral: ReferralService | None = None,
        streaks: StreakService | None = None,
    ) -> None:
        self.repos = repos
        self.points = points or PointsService(repos.economy)
        self.referral = referral or ReferralService(repos)
        self.streaks = streaks or StreakService(repos)
        self.safety = safety

    # ── main flow ────────────────────────────────────────────────────────────
    async def finalize(
        self,
        user: User,
        *,
        injury_text: str | None = None,
        lang: str = "ar",
        with_safety_ai: bool = True,
    ) -> OnboardingResult:
        # 1. safety first — it can restrict the plan we are about to compute
        screen: SafetyScreen | None = None
        if injury_text is not None:
            if self.safety is None:
                from app.services.safety import static_screen

                screen = static_screen(injury_text, lang)
                user.health_conditions = screen.conditions
                user.requires_medical_clearance = screen.red_flag
                user.preferences = {
                    **(user.preferences or {}),
                    "restricted_movements": screen.restricted,
                    "safe_alternatives": screen.alternatives,
                    "medical_referral": screen.referral,
                }
            elif with_safety_ai:
                screen = await self.safety.screen(user, injury_text, lang)
                await self.safety.apply_to_user(user, screen)
            else:
                screen = await self.safety.screen(user, injury_text, lang)
                await self.safety.apply_to_user(user, screen)

        # 2. metabolic plan
        targets = compute_targets(user)
        apply_targets(user, targets, reason="onboarding")

        # 3. lifecycle
        await self.repos.users.complete_onboarding(user)
        await self.repos.tracking.record_weight(user, float(user.weight_kg or 0))
        await self.repos.tracking.daily_log(user)

        # 4. economy & gamification
        welcome = await self.points.welcome_bonus(user)
        referral_outcome = await self.referral.qualify(user)
        badge = await self.repos.economy.award_badge(user.id, BadgeKey.PROFILE_COMPLETE)
        badges = [BadgeKey.PROFILE_COMPLETE.value] if badge else []
        await self.streaks.register_activity(user)

        # 5. default reminders
        created = await _create_default_reminders(self.repos, user)

        await self.repos.session.flush()
        return OnboardingResult(
            user=user,
            targets=targets,
            safety=screen,
            welcome=welcome,
            referral=referral_outcome,
            badges=badges,
            reminders_created=created,
        )

    # ── profile edits ────────────────────────────────────────────────────────
    async def recompute(self, user: User, *, reason: str = "profile update") -> NutritionTargets:
        targets = compute_targets(user)
        apply_targets(user, targets, reason=reason)
        await self.repos.session.flush()
        return targets

    async def change_goal(self, user: User, goal: str, custom_note: str | None = None) -> NutritionTargets:
        user.goal = goal
        user.goal_custom_note = custom_note
        return await self.recompute(user, reason=f"goal → {goal}")


async def _create_default_reminders(repos: Repos, user: User) -> int:
    """Seed sensible reminders; the user can turn each one off later."""
    from app.config import settings
    from app.constants import ReminderKind
    from app.db.models import ReminderRule

    if not settings.reminders_enabled:
        return 0

    # never touch ``user.reminders`` here: lazy-loading a relationship outside an
    # awaited context raises MissingGreenlet in async SQLAlchemy.
    existing_rows = (
        await repos.session.execute(
            select(ReminderRule.kind).where(ReminderRule.user_id == user.id)
        )
    ).scalars().all()
    existing = set(existing_rows)

    defaults = [
        (ReminderKind.WATER.value, {"interval_minutes": settings.water_reminder_interval_min, "enabled": True}),
        (ReminderKind.WEIGH_IN.value, {
            "hour": settings.default_weigh_in_hour,
            "minute": 0,
            "days_of_week": [settings.default_weigh_in_day],
            "enabled": True,
        }),
        (ReminderKind.WORKOUT.value, {"hour": settings.default_workout_hour, "minute": 0, "enabled": False}),
        (ReminderKind.MEAL_LOG.value, {"hour": 21, "minute": 0, "enabled": False}),
        (ReminderKind.PROGRESS_PHOTO.value, {"hour": 10, "minute": 0, "days_of_week": [0], "enabled": False}),
    ]
    from app.services.reminders import compute_next_run

    created = 0
    for kind, payload in defaults:
        if kind in existing:
            continue
        rule = ReminderRule(user_id=user.id, kind=kind, **payload)
        # arm the rule immediately: a reminder without next_run_at never fires
        if rule.enabled:
            rule.next_run_at = compute_next_run(rule, user)
        repos.session.add(rule)
        existing.add(kind)
        created += 1
    await repos.session.flush()
    return created


def summarize_for_user(result: OnboardingResult, lang: str = "ar") -> str:
    """Ready-to-send completion message."""
    from app.i18n import t

    plan = format_targets_ar(result.user, result.targets) if lang.startswith("ar") else _plan_en(
        result.user, result.targets
    )
    text = t(lang, "onb.complete", plan=plan)
    if result.welcome and result.welcome.points:
        text += (
            f"\n\n🎁 مكافأة الترحيب: <b>+{result.welcome.points} نقطة</b> (رصيدك: {result.welcome.balance})"
            if lang.startswith("ar")
            else f"\n\n🎁 Welcome bonus: <b>+{result.welcome.points} points</b> (balance: {result.welcome.balance})"
        )
    if result.safety and result.safety.restricted:
        text += (
            "\n\n" + t(
                lang,
                "onb.safety_warning",
                detail=result.safety.notes or "",
                referral=result.safety.referral or "—",
                restricted="، ".join(result.safety.restricted[:6]) or "—",
                alternatives="، ".join(result.safety.alternatives[:6]) or "—",
            )
        )
    return text


def _plan_en(user: User, targets: NutritionTargets) -> str:
    from app.services.goal_registry import get_goal
    from app.services.metabolism import bmi_label

    lines = [
        "<b>📊 Your numbers</b>",
        f"• Goal: {get_goal(targets.goal).label_en}",
        f"• BMR: {round(targets.bmr)} kcal",
        f"• TDEE: {round(targets.tdee)} kcal",
        f"• <b>Target: {targets.calories} kcal/day</b>",
        f"• Protein {targets.protein_g} g | Carbs {targets.carbs_g} g | Fats {targets.fats_g} g",
        f"• Water: {targets.water_ml} ml/day",
    ]
    if targets.bmi:
        lines.append(f"• BMI: {targets.bmi} ({bmi_label(targets.bmi, 'en')})")
    lines.extend(f"• ⚠️ {note}" for note in targets.notes)
    return "\n".join(lines)
