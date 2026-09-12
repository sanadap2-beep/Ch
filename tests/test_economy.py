"""Points ledger, token-metered pricing, daily/monthly caps, coach access."""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.ai.client import Usage
from app.config import settings
from app.constants import PointsReason, SubscriptionPlan
from app.db.repositories.economy import DailyLimitReached, InsufficientPoints
from app.services.points import points_from_usage, points_from_usd, task_price


async def test_welcome_bonus_is_paid_once(services, new_user) -> None:
    first = await services.points.welcome_bonus(new_user)
    assert first is not None
    assert first.points == settings.welcome_bonus_points
    assert new_user.points_balance >= settings.welcome_bonus_points

    second = await services.points.welcome_bonus(new_user)
    assert second is None                      # never paid twice


@pytest.mark.parametrize("prompt,completion,expected_floor,expected_ceiling", [
    (0, 0, settings.coach_points_minimum, settings.coach_points_minimum),
    (2_000, 1_000, settings.coach_points_minimum, settings.coach_points_maximum),
    (500_000, 500_000, settings.coach_points_maximum, settings.coach_points_maximum),
])
def test_token_metered_price_stays_inside_the_configured_band(
    prompt: int, completion: int, expected_floor: int, expected_ceiling: int
) -> None:
    usage = Usage(prompt_tokens=prompt, completion_tokens=completion, total_tokens=prompt + completion)
    price = points_from_usage(usage)
    assert expected_floor <= price <= expected_ceiling


def test_token_price_grows_with_output_tokens() -> None:
    small = points_from_usage(Usage(prompt_tokens=300, completion_tokens=200))
    large = points_from_usage(Usage(prompt_tokens=3_000, completion_tokens=4_000))
    assert large >= small


def test_points_from_usd_and_task_price() -> None:
    assert points_from_usd(0) == 0
    assert points_from_usd(1.0) == settings.points_per_usd
    assert points_from_usd(0.011) >= 1                       # never free once spent
    assert task_price("coach_access") == settings.coach_access_cost_points
    assert task_price("vision_food") == settings.food_scan_cost_points
    assert task_price("unknown_task") == 0


async def test_charge_ai_turn_writes_a_ledger_entry(services, user) -> None:
    before = int(user.points_balance)
    usage = Usage(prompt_tokens=1_200, completion_tokens=900, total_tokens=2_100, cost_usd=0.004)
    charge = await services.points.charge_ai_turn(user, usage, model="deepseek/deepseek-v4-pro")

    assert charge.points < 0
    assert user.points_balance == before + charge.points
    assert charge.entry_id is not None

    entries = await services.repos.economy.ledger(user.id, limit=5)
    assert entries[0].reason == PointsReason.COACH_MESSAGE.value
    assert entries[0].balance_after == user.points_balance
    assert entries[0].reference["model"] == "deepseek/deepseek-v4-pro"


async def test_spend_never_drives_the_balance_negative(services, user) -> None:
    user.points_balance = 3
    with pytest.raises(InsufficientPoints) as excinfo:
        await services.points.charge_flat(user, 100, PointsReason.COACH_ACCESS)
    assert excinfo.value.required == 100
    assert excinfo.value.available == 3
    assert user.points_balance == 3                          # nothing was deducted


async def test_admin_grant_and_deduct_record_the_actor(services, user) -> None:
    granted = await services.points.admin_grant(user, 500, admin_tg_id=12345, note="شحن يدوي")
    assert granted.points == 500
    deducted = await services.points.admin_deduct(user, 200, admin_tg_id=12345)
    assert deducted.points == -200

    entries = await services.repos.economy.ledger(user.id, limit=5)
    reasons = {entry.reason for entry in entries}
    assert PointsReason.ADMIN_GRANT.value in reasons
    assert PointsReason.ADMIN_DEDUCT.value in reasons
    actors = {entry.actor_tg_id for entry in entries if entry.actor_tg_id}
    assert 12345 in actors


async def test_admin_deduct_may_go_negative_only_when_allowed(services, user) -> None:
    user.points_balance = 10
    charge = await services.points.admin_deduct(user, 50, admin_tg_id=1)
    assert user.points_balance == charge.balance == -40       # correction tool


async def test_food_scans_are_free_then_paid(services, user) -> None:
    free_results = []
    for _ in range(settings.food_scan_free_daily):
        free_results.append(await services.points.charge_food_scan(user))
    assert all(result.points == 0 for result in free_results)

    paid = await services.points.charge_food_scan(user)
    assert paid.points == -settings.food_scan_cost_points
    assert user.daily_food_scans_free_used == settings.food_scan_free_daily


async def test_unlock_coach_grants_access_and_charges(services, user) -> None:
    assert user.has_coach_access is False
    before = int(user.points_balance)
    charge = await services.points.unlock_coach(user)

    assert charge.points == -settings.coach_access_cost_points
    assert user.points_balance == before - settings.coach_access_cost_points
    assert user.has_coach_access is True
    assert user.coach_access_until is not None
    if settings.coach_access_days:
        assert user.coach_access_until.date() >= (date.today() + timedelta(days=1))


async def test_daily_counter_cap_blocks_further_consults(services, user) -> None:
    await services.repos.economy.ensure_windows(user)
    user.daily_consult_count = settings.consult_free_daily_limit
    with pytest.raises(DailyLimitReached) as excinfo:
        await services.repos.economy.check_limits(user, "consult")
    assert excinfo.value.kind == "consult"


async def test_daily_points_cap_blocks_spending(services, user) -> None:
    await services.repos.economy.ensure_windows(user)
    user.daily_points_spent = settings.user_daily_points_cap
    with pytest.raises(DailyLimitReached) as excinfo:
        await services.repos.economy.check_limits(user, "coach")
    assert excinfo.value.kind == "points_daily"


async def test_monthly_points_cap_blocks_spending(services, user) -> None:
    await services.repos.economy.ensure_windows(user)
    user.monthly_points_spent = settings.user_monthly_points_cap
    with pytest.raises(DailyLimitReached) as excinfo:
        await services.repos.economy.check_limits(user, "vision")
    assert excinfo.value.kind == "points_monthly"


async def test_usage_windows_reset_on_a_new_local_day(services, user) -> None:
    await services.repos.economy.ensure_windows(user)
    user.daily_consult_count = 9
    user.daily_coach_messages = 30
    user.daily_vision_count = 20
    user.daily_points_spent = 700
    user.usage_day = user.local_today() - timedelta(days=1)
    user.usage_month = (user.local_today() - timedelta(days=40)).strftime("%Y-%m")

    await services.repos.economy.ensure_windows(user)

    assert user.daily_consult_count == 0
    assert user.daily_coach_messages == 0
    assert user.daily_vision_count == 0
    assert user.daily_points_spent == 0
    assert user.monthly_points_spent == 0
    assert user.usage_day == user.local_today()


async def test_ledger_pagination_and_counts(services, user) -> None:
    for index in range(12):
        await services.points.admin_grant(user, 10 + index, admin_tg_id=1)

    total = await services.repos.economy.ledger_count(user.id)
    assert total >= 12
    page_one = await services.repos.economy.ledger(user.id, limit=5, offset=0)
    page_two = await services.repos.economy.ledger(user.id, limit=5, offset=5)
    assert len(page_one) == 5 and len(page_two) == 5
    assert {entry.id for entry in page_one}.isdisjoint({entry.id for entry in page_two})

    totals = await services.repos.economy.totals()
    assert totals[PointsReason.ADMIN_GRANT.value]["count"] >= 12
    assert totals[PointsReason.ADMIN_GRANT.value]["points"] > 0


async def test_subscription_grant_pays_and_sets_expiry(services, user) -> None:
    charge = await services.subscription.activate(
        user, SubscriptionPlan.MONTHLY.value, admin_tg_id=1
    )
    assert charge is not None
    assert charge.points == settings.monthly_plan_points
    assert user.has_subscription is True
    assert user.subscription_plan == SubscriptionPlan.MONTHLY.value
    assert user.subscription_expires_at is not None


async def test_subscription_cancellation_keeps_history(services, user) -> None:
    await services.subscription.activate(user, SubscriptionPlan.MONTHLY.value, admin_tg_id=1)
    await services.subscription.cancel(user, admin_tg_id=1)
    assert user.has_subscription is False


async def test_due_subscriptions_are_found_for_renewal(services, user) -> None:
    """Renewals fire while the plan is still active and 29+ days passed since the last grant."""
    from app.db.models import utcnow

    await services.subscription.activate(user, SubscriptionPlan.MONTHLY.value, admin_tg_id=1)
    # freshly granted → not due yet
    assert not any(item.id == user.id for item in await services.repos.economy.due_subscriptions())

    user.subscription_last_grant_at = utcnow() - timedelta(days=31)
    await services.repos.session.flush()

    due = await services.repos.economy.due_subscriptions()
    assert any(item.id == user.id for item in due)

    balance_before = int(user.points_balance)
    results = await services.subscription.run_renewals()
    assert any(item.tg_id == user.tg_id and item.ok for item in results)
    assert user.points_balance == balance_before + settings.monthly_plan_points
    assert user.subscription_last_grant_at > utcnow() - timedelta(minutes=1)


async def test_cancelled_and_banned_users_are_never_renewed(services, user) -> None:
    from app.db.models import utcnow

    await services.subscription.activate(user, SubscriptionPlan.MONTHLY.value, admin_tg_id=1)
    user.subscription_last_grant_at = utcnow() - timedelta(days=31)
    await services.subscription.cancel(user, admin_tg_id=1)
    assert not any(item.id == user.id for item in await services.repos.economy.due_subscriptions())

    await services.subscription.activate(user, SubscriptionPlan.MONTHLY.value, admin_tg_id=1)
    user.subscription_last_grant_at = utcnow() - timedelta(days=31)
    user.is_banned = True
    await services.repos.session.flush()
    assert not any(item.id == user.id for item in await services.repos.economy.due_subscriptions())
