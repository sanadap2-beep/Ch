"""Daily log arithmetic, meals, water, weight trend and streak state."""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.constants import MealKind


async def test_daily_log_is_created_from_the_user_targets(services, user) -> None:
    log = await services.repos.tracking.daily_log(user, user.local_today())
    assert log.target_calories == user.target_calories
    assert log.target_protein_g == user.target_protein_g
    assert log.remaining_calories == user.target_calories
    assert log.log_date == user.local_today()

    again = await services.repos.tracking.daily_log(user, user.local_today())
    assert again.id == log.id                       # one row per user per day


async def test_add_meal_updates_the_daily_balance(services, user) -> None:
    today = user.local_today()
    await services.vision.log_manual(
        user, name="شاورما", calories=600, protein_g=35.0, carbs_g=55.0, fats_g=22.0
    )
    log = await services.repos.tracking.daily_log(user, today)

    assert log.consumed_calories == 600
    assert log.remaining_calories == user.target_calories - 600
    assert log.meals_logged == 1
    assert log.remaining_macros["protein_g"] == pytest.approx(user.target_protein_g - 35.0, abs=0.5)

    meals = await services.repos.tracking.meals_of_day(user.id, today)
    assert len(meals) == 1
    assert meals[0].meal_name == "شاورما"
    assert meals[0].kind == MealKind.MANUAL.value
    assert user.total_meals_logged == 1


async def test_two_meals_accumulate_and_never_go_below_zero(services, user) -> None:
    target = user.target_calories
    await services.vision.log_manual(user, name="فطور", calories=target - 100)
    await services.vision.log_manual(user, name="غدا", calories=500)
    log = await services.repos.tracking.daily_log(user, user.local_today())
    assert log.consumed_calories == target + 400
    assert log.remaining_calories <= 0                # overspend is visible, not hidden
    assert log.meals_logged == 2


async def test_add_water_accumulates(services, user) -> None:
    today = user.local_today()
    await services.repos.tracking.add_water(user, today, 500)
    log = await services.repos.tracking.add_water(user, today, 250)
    assert log.water_ml == 750
    assert log.water_target_ml == user.water_target_ml


async def test_log_skipped_records_the_missed_meal(services, user) -> None:
    state = await services.vision.log_skipped_meal(user, slot="lunch", reason="كنت بالدوام")
    log = await services.repos.tracking.daily_log(user, user.local_today())
    assert log.skipped_meals and log.skipped_meals[0]["slot"] == "lunch"
    assert "remaining_calories" in state


async def test_set_workout_done_counts_once(services, user) -> None:
    before = int(user.total_workouts or 0)
    await services.repos.tracking.set_workout_done(user, calories=250)
    assert user.total_workouts == before + 1
    await services.repos.tracking.set_workout_done(user, calories=100)   # second tap, same day
    assert user.total_workouts == before + 1                            # not double counted

    log = await services.repos.tracking.daily_log(user, user.local_today())
    assert log.workout_done is True
    assert log.burned_calories_est == 350


async def test_record_weight_updates_profile_and_history(services, user) -> None:
    await services.repos.tracking.record_weight(user, 85.2, note="morning")
    assert user.weight_kg == pytest.approx(85.2)

    record = await services.repos.tracking.record_weight(user, 84.9)     # same day → overwrite
    history = await services.repos.tracking.weight_history(user.id)
    assert len(history) == 1
    assert record.weight_kg == pytest.approx(84.9)

    latest = await services.repos.tracking.latest_weight(user.id)
    assert latest is not None and latest.weight_kg == pytest.approx(84.9)


async def test_weight_trend_detects_a_stall(services, user) -> None:
    today = user.local_today()
    await services.repos.tracking.record_weight(user, 86.0, day=today - timedelta(days=10))
    await services.repos.tracking.record_weight(user, 86.05, day=today - timedelta(days=3))
    trend = await services.repos.tracking.weight_trend(user.id, weeks=2)
    assert trend["samples"] == 2
    assert trend["stalled"] is True
    assert abs(trend["per_week_kg"]) < 0.15


async def test_weight_trend_needs_two_samples(services, user) -> None:
    trend = await services.repos.tracking.weight_trend(user.id)
    assert trend["samples"] <= 1
    assert trend["per_week_kg"] == 0.0


async def test_record_and_read_measurements(services, user) -> None:
    await services.repos.tracking.record_measurements(user, {"waist_cm": 92.0, "chest_cm": 104.0})
    history = await services.repos.tracking.measurement_history(user.id)
    assert history
    assert history[0].waist_cm == pytest.approx(92.0)


async def test_progress_photos_round_trip(services, user) -> None:
    media = await services.media.store_image(
        b"\xff\xd8\xff\xe0" + b"0" * 64, user_id=user.id, kind="progress", optimise=False
    )
    record = media[0]
    photo = await services.repos.tracking.add_photo(
        user, media_id=record.id if record else None, angle="front", consented=True
    )
    assert photo.weight_at_time_kg == pytest.approx(user.weight_kg)   # stamped from the profile
    photos = await services.repos.tracking.photos(user.id)
    assert len(photos) == 1
    assert photos[0].id == photo.id

    removed = await services.repos.tracking.delete_user_photos(user.id)
    assert removed >= 1
    assert not await services.repos.tracking.photos(user.id)


async def test_summary_between_aggregates_a_period(services, user) -> None:
    today = user.local_today()
    await services.vision.log_manual(user, name="وجبة", calories=700, protein_g=40.0)
    await services.repos.tracking.add_water(user, today, 500)
    await services.repos.tracking.set_workout_done(user, calories=200)

    summary = await services.repos.tracking.summary_between(user.id, today - timedelta(days=6), today)
    assert summary["days"] >= 1
    assert summary["avg_calories"] > 0
    assert summary["workouts"] >= 1


async def test_streak_state_counts_good_days(services, user) -> None:
    """A "good day" needs real adherence (calories + protein + water + training)."""
    today = user.local_today()
    for offset in (2, 1, 0):
        day = today - timedelta(days=offset)
        await services.repos.tracking.add_meal(
            user, day, meal_name="وجبة", calories=int(user.target_calories * 0.95),
            protein_g=float(user.target_protein_g), carbs_g=float(user.target_carbs_g),
            fats_g=float(user.target_fats_g),
        )
        await services.repos.tracking.add_water(user, day, int(user.water_target_ml))
        await services.repos.tracking.set_workout_done(user, day=day, calories=200)

    state = await services.repos.tracking.streak_state(user.id)
    assert state["good_days"] == 3
    assert state["current"] >= 1


async def test_days_since_last_weigh_in(services, user) -> None:
    assert await services.repos.tracking.days_since_last_weigh_in(user.id) is None
    await services.repos.tracking.record_weight(user, 85.0, day=user.local_today() - timedelta(days=4))
    assert await services.repos.tracking.days_since_last_weigh_in(user.id) == 4


async def test_sleep_mood_and_steps_are_stored(services, user) -> None:
    log = await services.repos.tracking.set_sleep_mood(
        user, sleep_hours=7.5, mood="good", soreness=3, steps=8200
    )
    assert log.sleep_hours == pytest.approx(7.5)
    assert log.steps == 8200
    assert log.soreness == 3


async def test_meal_deletion_restores_the_balance(services, user) -> None:
    today = user.local_today()
    entry = await services.repos.tracking.add_meal(
        user, today, meal_name="وجبة", calories=400, protein_g=20.0, carbs_g=30.0, fats_g=10.0
    )
    log = await services.repos.tracking.daily_log(user, today)
    assert log.consumed_calories == 400

    await services.repos.tracking.delete_meal(entry)
    log = await services.repos.tracking.daily_log(user, today)
    assert log.consumed_calories == 0
    assert log.remaining_calories == user.target_calories


async def test_log_for_returns_none_for_an_unknown_day(services, user) -> None:
    assert await services.repos.tracking.log_for(user.id, date(2001, 1, 1)) is None
