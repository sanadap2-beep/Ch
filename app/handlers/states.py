"""FSM states for every multi-step conversation."""
from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class Onboarding(StatesGroup):
    """Spec §2 — the six-step sign-up flow (+ budget/food questions)."""

    name = State()
    age = State()
    gender = State()
    height = State()
    weight = State()
    target_weight = State()
    activity = State()
    goal = State()
    goal_custom = State()
    equipment = State()
    injuries = State()
    budget = State()
    food_style = State()
    food_custom = State()
    disliked = State()


class ProfileEdit(StatesGroup):
    field = State()          # which field is being edited (stored in FSM data)
    timezone = State()
    injuries = State()
    goal_custom = State()
    food_custom = State()


class ConsultFlow(StatesGroup):
    question = State()


class CoachFlow(StatesGroup):
    skip_meal = State()      # which meal was skipped
    manual_meal = State()    # "name calories protein" style entry
    feedback = State()       # post-workout feedback


class ScanFlow(StatesGroup):
    waiting_photo = State()
    clarification = State()  # user is answering the vision model's questions
    manual = State()


class ProgressFlow(StatesGroup):
    weight = State()
    measurements = State()
    photo_angle = State()
    waiting_photo = State()
    photo_caption = State()


class PlanFlow(StatesGroup):
    instructions = State()


class ReminderFlow(StatesGroup):
    time = State()
    days = State()


class AdminFlow(StatesGroup):
    search = State()
    amount = State()
    ban_reason = State()
    broadcast = State()
    channels = State()
    config = State()
    note = State()
