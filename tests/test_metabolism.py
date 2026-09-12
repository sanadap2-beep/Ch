"""Mifflin-St Jeor BMR/TDEE, macro split, safety clamps, stall recalibration."""
from __future__ import annotations

import pytest

from app.constants import ActivityLevel, Gender, Goal
from app.services.metabolism import (
    adjusted_body_weight,
    apply_targets,
    bmi,
    bmi_label,
    bmr_mifflin,
    clamp_calories,
    compute_targets,
    days_to_goal,
    expected_weight,
    macro_split,
    protein_needs,
    recalibrate,
    redistribute_remaining,
    tdee_from,
    water_target_ml,
)


def test_bmr_mifflin_matches_reference_formula() -> None:
    # male: 10*86 + 6.25*178 - 5*28 + 5 = 860 + 1112.5 - 140 + 5
    assert bmr_mifflin(86, 178, 28, Gender.MALE.value) == pytest.approx(1837.5)
    # female: -161 instead of +5 → 1671.5
    assert bmr_mifflin(86, 178, 28, Gender.FEMALE.value) == pytest.approx(1671.5)
    assert bmr_mifflin(86, 178, 28, Gender.MALE.value) - bmr_mifflin(
        86, 178, 28, Gender.FEMALE.value) == pytest.approx(166.0)


def test_tdee_activity_multipliers_are_ordered() -> None:
    bmr = 1800.0
    values = [tdee_from(bmr, level.value) for level in ActivityLevel]
    assert values == sorted(values)
    assert values[0] > bmr                      # even sedentary burns more than BMR
    assert values[-1] > values[0] * 1.3         # athlete ≫ sedentary


def test_tdee_unknown_activity_falls_back_to_light() -> None:
    assert tdee_from(1800.0, None) == tdee_from(1800.0, ActivityLevel.LIGHT.value)


def test_bmi_and_label() -> None:
    value = bmi(86, 178)
    assert value == pytest.approx(27.1, abs=0.1)
    assert bmi_label(value, "ar")
    assert bmi_label(value, "en")
    assert bmi(None, 178) is None
    assert bmi_label(None, "en") == ""          # unknown BMI → no label


def test_adjusted_body_weight_uses_lean_mass_for_obese_users() -> None:
    normal = adjusted_body_weight(75, 180)
    obese = adjusted_body_weight(150, 170)
    assert normal == pytest.approx(75)
    assert obese < 150                          # never feed protein targets off total mass


def test_water_target_scales_with_weight() -> None:
    assert water_target_ml(60) < water_target_ml(100)
    assert water_target_ml(None) > 0


def test_macro_split_covers_every_goal_profile() -> None:
    from app.services.goal_registry import get_goal

    for goal in Goal:
        profile = get_goal(goal.value)
        protein_g, carbs_g, fats_g = macro_split(
            2000, 85, protein_g_per_kg=profile.protein_g_per_kg,
            fat_g_per_kg=profile.fat_g_per_kg, carb_style=profile.carb_style, height_cm=178,
        )
        kcal = protein_g * 4 + carbs_g * 4 + fats_g * 9
        assert protein_g >= 60, goal.value           # the configured protein floor
        assert carbs_g >= 40, goal.value             # never a crash-level carb cut
        assert kcal == pytest.approx(2000, rel=0.12), goal.value


def test_macro_split_respects_the_hormonal_fat_floor() -> None:
    from app.services.goal_registry import get_goal

    profile = get_goal(Goal.CUT.value)
    _protein, _carbs, fats = macro_split(
        1400, 100, protein_g_per_kg=profile.protein_g_per_kg,
        fat_g_per_kg=profile.fat_g_per_kg, carb_style="low", height_cm=175,
    )
    assert fats >= round(100 * 0.6)                  # ≥0.6 g/kg even in an aggressive cut


def test_clamp_calories_protects_minimums() -> None:
    low, notes = clamp_calories(800, Gender.MALE.value)
    assert low >= 1500
    assert notes                                  # the user is told why it was raised
    high, _ = clamp_calories(9000, Gender.FEMALE.value)
    assert high <= 4500


def test_compute_targets_is_deterministic_for_the_same_profile(user) -> None:
    first = compute_targets(user)
    second = compute_targets(user)
    assert first.calories == second.calories
    assert first.protein_g == second.protein_g
    assert first.tdee > first.bmr
    # a cut must sit below maintenance
    assert first.calories < first.tdee


def test_compute_targets_goal_override_changes_calories(user) -> None:
    cut = compute_targets(user, goal_override=Goal.CUT.value)
    bulk = compute_targets(user, goal_override=Goal.BULK.value)
    assert bulk.calories > cut.calories


def test_apply_targets_writes_the_plan_onto_the_user(user) -> None:
    targets = compute_targets(user)
    apply_targets(user, targets, reason="test")
    assert user.target_calories == targets.calories
    assert user.target_protein_g == targets.protein_g
    assert user.water_target_ml == targets.water_ml
    assert user.plan_adjustments                      # an auditable history entry
    assert user.plan_adjustments[-1]["reason"] == "test"


def test_recalibrate_reacts_to_a_two_week_stall(user) -> None:
    user.target_calories = 2000
    user.stall_weeks = 0
    stalled = {"samples": 4, "per_week_kg": 0.0, "stalled": True, "weeks": 3}
    result = recalibrate(user, stalled)
    assert result.changed is True
    assert result.new_calories != 2000
    assert abs(result.new_calories - 2000) >= 100      # a meaningful adjustment


def test_recalibrate_needs_at_least_two_weigh_ins(user) -> None:
    user.target_calories = 2000
    result = recalibrate(user, {"samples": 1, "per_week_kg": 0.0, "stalled": True})
    assert result.changed is False
    assert result.reason == "not enough weigh-ins"


def test_recalibrate_leaves_progressing_users_alone(user) -> None:
    user.target_calories = 2000
    result = recalibrate(user, {"samples": 4, "per_week_kg": -0.6, "stalled": False, "weeks": 2})
    assert result.changed is False
    assert user.target_calories == 2000


def test_expected_weight_and_days_to_goal() -> None:
    assert expected_weight(90, 80, 14, rate_per_week=0.5) == pytest.approx(89.0)
    assert days_to_goal(90, 80, rate_per_week=0.5) == pytest.approx(140, abs=1)
    assert days_to_goal(80, 80) == 0


def test_protein_needs_rise_for_a_cut() -> None:
    assert protein_needs(85, Goal.CUT.value, 178) > protein_needs(85, Goal.GENERAL_HEALTH.value, 178)


def test_redistribute_remaining_front_loads_protein() -> None:
    meals = redistribute_remaining(
        remaining_calories=1200, remaining_protein_g=90.0, remaining_carbs_g=120.0,
        remaining_fats_g=40.0, meals_left=3,
    )
    assert len(meals) == 3
    assert sum(m["calories"] for m in meals) == pytest.approx(1200, rel=0.05)
    assert meals[0]["protein_g"] >= meals[-1]["protein_g"]
    assert [m["meal_index"] for m in meals] == [1, 2, 3]


def test_redistribute_remaining_never_divides_by_zero() -> None:
    meals = redistribute_remaining(
        remaining_calories=500, remaining_protein_g=40.0, remaining_carbs_g=50.0,
        remaining_fats_g=15.0, meals_left=0,
    )
    assert len(meals) == 1
