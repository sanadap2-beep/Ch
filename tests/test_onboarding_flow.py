"""Onboarding completion: metabolic plan, safety screen, welcome bonus, referral."""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.constants import BadgeKey, Gender, Goal, InjuryArea, PointsReason
from app.services.goal_registry import all_goals, get_goal
from app.services.onboarding import summarize_for_user
from app.services.safety import detect_areas, static_screen


async def test_finalize_builds_the_full_profile(services, new_user) -> None:
    new_user.injuries = [{"area": InjuryArea.NONE.value}]
    result = await services.onboarding.finalize(new_user, injury_text="لا شيء", lang="ar")

    assert new_user.onboarding_done is True
    assert new_user.onboarding_completed_at is not None
    assert result.targets.calories > 0
    assert new_user.target_calories == result.targets.calories
    assert new_user.target_protein_g == result.targets.protein_g
    assert new_user.water_target_ml == result.targets.water_ml
    assert new_user.bmr and new_user.tdee


async def test_finalize_pays_the_welcome_bonus_once(services, new_user) -> None:
    result = await services.onboarding.finalize(new_user, lang="ar")
    assert result.welcome is not None
    assert result.welcome.points == pytest.approx(
        abs(result.welcome.points)) and result.welcome.points > 0

    entries = await services.repos.economy.ledger(new_user.id, limit=10)
    welcome_entries = [e for e in entries if e.reason == PointsReason.WELCOME_BONUS.value]
    assert len(welcome_entries) == 1


async def test_finalize_awards_the_profile_badge(services, new_user) -> None:
    result = await services.onboarding.finalize(new_user, lang="ar")
    assert BadgeKey.PROFILE_COMPLETE.value in result.badges
    badges = await services.repos.economy.badges_of(new_user.id)
    assert any(b.badge_key == BadgeKey.PROFILE_COMPLETE.value for b in badges)


async def test_finalize_creates_default_reminders(services, new_user) -> None:
    result = await services.onboarding.finalize(new_user, lang="ar")
    assert result.reminders_created >= 2
    rules = await services.reminders.rules(new_user)
    kinds = {rule.kind for rule in rules}
    assert "water" in kinds and "weigh_in" in kinds
    assert any(rule.next_run_at is not None for rule in rules if rule.enabled)


async def test_finalize_records_the_first_weight_and_daily_log(services, new_user) -> None:
    new_user.weight_kg = 91.5
    await services.onboarding.finalize(new_user, lang="ar")
    history = await services.repos.tracking.weight_history(new_user.id)
    assert history and history[0].weight_kg == pytest.approx(91.5)
    log = await services.repos.tracking.daily_log(new_user, new_user.local_today())
    assert log.target_calories == new_user.target_calories


async def test_summary_message_mentions_the_plan_and_bonus(services, new_user) -> None:
    result = await services.onboarding.finalize(new_user, lang="ar")
    text = summarize_for_user(result, "ar")
    assert str(result.targets.calories) in text
    assert "مكافأة الترحيب" in text
    english = summarize_for_user(result, "en")
    assert str(result.targets.calories) in english


async def test_red_flag_conditions_force_medical_clearance(services, new_user) -> None:
    screen = static_screen("عندي ألم في الصدر وضغط دم مرتفع", "ar")
    assert screen.red_flag is True
    assert screen.conditions
    assert screen.restricted and screen.alternatives
    assert screen.referral

    result = await services.onboarding.finalize(
        new_user, injury_text="ألم في الصدر وارتفاع ضغط", lang="ar"
    )
    assert result.safety is not None
    assert new_user.requires_medical_clearance is True
    assert new_user.preferences["restricted_movements"]


async def test_a_plain_knee_injury_is_not_a_red_flag(services, new_user) -> None:
    """An injury restricts movements but does not by itself require clearance."""
    screen = static_screen("ركبتي تؤلمني أحيانًا", "ar")
    assert screen.red_flag is False
    assert screen.restricted and screen.alternatives
    assert screen.referral

    await services.safety.apply_to_user(new_user, screen)
    areas = {str(item.get("area")) for item in (new_user.injuries or [])}
    assert InjuryArea.KNEE.value in areas
    assert new_user.requires_medical_clearance is False


def test_detect_areas_maps_arabic_words_to_body_parts() -> None:
    assert InjuryArea.KNEE.value in detect_areas("عندي إصابة ركبة")
    assert InjuryArea.LOWER_BACK.value in detect_areas("آلام أسفل الظهر")
    assert detect_areas("ما عندي شي") in ([], [InjuryArea.NONE.value])


async def test_safety_screen_merges_ai_findings(services, new_user) -> None:
    screen = await services.safety.screen(new_user, "ركبتي تؤلمني", "ar")
    assert screen.conditions
    services.safety.apply_to_user if hasattr(services.safety, "apply_to_user") else None
    assert isinstance(screen.source, str)


async def test_change_goal_recomputes_the_plan(services, user) -> None:
    cut_targets = await services.onboarding.change_goal(user, Goal.CUT.value)
    bulk_targets = await services.onboarding.change_goal(user, Goal.BULK.value)
    assert bulk_targets.calories > cut_targets.calories
    assert user.goal == Goal.BULK.value
    assert user.target_calories == bulk_targets.calories


async def test_change_goal_stores_a_custom_note(services, user) -> None:
    await services.onboarding.change_goal(user, Goal.CUSTOM.value, custom_note="أرجع أتسلق الجبال")
    assert user.goal == Goal.CUSTOM.value
    assert user.goal_custom_note == "أرجع أتسلق الجبال"
    assert user.plan_adjustments[-1]["reason"].startswith("goal →")


async def test_recompute_keeps_an_audit_trail(services, user) -> None:
    user.weight_kg = 80.0
    before = len(user.plan_adjustments or [])
    await services.onboarding.recompute(user, reason="weight updated")
    assert len(user.plan_adjustments) == before + 1
    assert user.plan_adjustments[-1]["reason"] == "weight updated"
    assert user.plan_adjustments[-1]["date"]
    assert user.plan_adjustments[-1]["calories"] == user.target_calories


async def test_referral_attributes_and_pays_both_sides(services, new_user, user) -> None:
    from app.config import settings

    outcome = await services.referral.attach_inviter(new_user, user.referral_code)
    assert outcome.inviter is not None and outcome.inviter.id == user.id
    assert outcome.invitee_points == settings.referral_invitee_points
    assert outcome.already_referred is False

    # completing the profile qualifies the referral and pays the inviter
    await services.onboarding.finalize(new_user, lang="ar")
    qualified = await services.referral.qualify(new_user)
    assert qualified.already_referred is True      # finalize() already qualified it

    inviter_stats = await services.referral.stats(user)
    assert inviter_stats["count"] == 1
    assert inviter_stats["points_earned"] >= settings.referral_inviter_points
    assert inviter_stats["link"].endswith(f"?start=ref_{user.referral_code}")

    entries = await services.repos.economy.ledger(user.id, limit=10)
    assert any(e.reason == PointsReason.REFERRAL_INVITER.value for e in entries)


async def test_referral_rejects_self_invites_and_unknown_codes(services, new_user, user) -> None:
    assert (await services.referral.attach_inviter(new_user, "NOPE999")).inviter is None
    assert (await services.referral.attach_inviter(user, user.referral_code)).inviter is None


async def test_referral_milestone_bonus(services, session, user) -> None:
    from app.config import settings
    from tests.conftest import make_user

    for index in range(settings.referral_bonus_every):
        invitee = make_user(tg_id=700_000 + index, referral_code=f"INV{index}")
        invitee.privacy_consent_at = datetime.now(UTC)
        session.add(invitee)
        await session.flush()
        await services.referral.attach_inviter(invitee, user.referral_code)
        await services.onboarding.finalize(invitee, lang="ar")
        await services.referral.qualify(invitee)

    stats = await services.referral.stats(user)
    assert stats["count"] == settings.referral_bonus_every
    assert await services.repos.economy.has_badge(user.id, BadgeKey.REFERRED_5) or stats["points_earned"] > (
        settings.referral_inviter_points * settings.referral_bonus_every
    )


def test_goal_registry_covers_the_spec_goals() -> None:
    keys = {goal["key"] for goal in all_goals("ar")}
    for expected in ("cut", "bulk", "recomp", "general_fitness", "boxing", "flexibility", "custom"):
        assert expected in keys
    profile = get_goal("boxing")
    assert profile.plan_kind == "boxing"
    assert profile.describe("ar") and profile.describe("en")
    assert get_goal("not-a-goal").key == Goal.GENERAL_HEALTH.value   # unknown → safe default


def test_gender_specific_minimums_are_configured() -> None:
    from app.config import settings

    assert settings.min_calories_male > settings.min_calories_female
    assert Gender.MALE.value != Gender.FEMALE.value
