"""SQLAlchemy 2.0 declarative models — the whole persistence layer.

Design notes
------------
* **Portable column types.**  Production runs on PostgreSQL (``JSONB``, native
  ``UUID``), but every type has a SQLite ``variant`` so the test-suite and a
  quick local demo run without a database server.
* **Enums are stored as ``String``**, not as native PG enums.  This is
  deliberate: the spec requires that new goals / coaches can be added later
  *without rebuilding the schema*.  Validation happens in Python
  (:mod:`app.constants`) and in the service layer.
* **Money and points are integers.**  Points are the internal currency; USD
  cost is a ``Numeric`` used only for observability.
"""
from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# JSONB on PostgreSQL, plain JSON everywhere else (SQLite tests / dev).
JSONType = JSONB().with_variant(JSON(), "sqlite")


def utcnow() -> datetime:
    return datetime.now(UTC)


def as_aware(value: datetime | None) -> datetime | None:
    """SQLite hands back naive datetimes; PostgreSQL returns aware ones.

    Comparing the two raises ``TypeError``, so every datetime that came out of
    the database is normalised to UTC-aware before it is compared or subtracted.
    """
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def new_uuid() -> uuid.UUID:
    return uuid.uuid4()


class Base(DeclarativeBase):
    type_annotation_map = {
        dict[str, Any]: JSONType,
        list[Any]: JSONType,
    }


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, server_default=func.now(), nullable=False
    )


# ═══════════════════════════════════════════════════════════════════════════
#  USER
# ═══════════════════════════════════════════════════════════════════════════
class User(Base, TimestampMixin):
    """One Telegram user = one coaching client."""

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=new_uuid)
    tg_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True, nullable=False)
    username: Mapped[str | None] = mapped_column(String(64), index=True)
    first_name: Mapped[str | None] = mapped_column(String(128))
    last_name: Mapped[str | None] = mapped_column(String(128))
    language_code: Mapped[str] = mapped_column(String(8), default="ar")   # ar | en

    # ── onboarding profile ───────────────────────────────────────────────────
    display_name: Mapped[str | None] = mapped_column(String(128))
    age: Mapped[int | None] = mapped_column(Integer)
    gender: Mapped[str | None] = mapped_column(String(16))                # male | female
    height_cm: Mapped[float | None] = mapped_column(Float)
    weight_kg: Mapped[float | None] = mapped_column(Float)                # latest known
    target_weight_kg: Mapped[float | None] = mapped_column(Float)
    activity_level: Mapped[str | None] = mapped_column(String(24))
    goal: Mapped[str | None] = mapped_column(String(32), index=True)
    goal_custom_note: Mapped[str | None] = mapped_column(Text)
    equipment: Mapped[str | None] = mapped_column(String(32))
    experience_level: Mapped[str | None] = mapped_column(String(24))      # beginner|intermediate|advanced
    budget_level: Mapped[str | None] = mapped_column(String(16))
    food_style: Mapped[str | None] = mapped_column(String(32))
    food_style_note: Mapped[str | None] = mapped_column(Text)             # "أكل شعبي سوري/مصري…"
    disliked_foods: Mapped[list[str] | None] = mapped_column(JSONType, default=list)
    allergies: Mapped[list[str] | None] = mapped_column(JSONType, default=list)
    meals_per_day: Mapped[int | None] = mapped_column(Integer)

    # ── health & safety (spec §2.5, §5.6) ────────────────────────────────────
    injuries: Mapped[list[dict[str, Any]]] = mapped_column(JSONType, default=list)
    health_conditions: Mapped[list[str]] = mapped_column(JSONType, default=list)
    medical_notes: Mapped[str | None] = mapped_column(Text)
    requires_medical_clearance: Mapped[bool] = mapped_column(Boolean, default=False)
    safety_warning_shown_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # ── metabolic plan (recalculated automatically) ──────────────────────────
    bmr: Mapped[float | None] = mapped_column(Float)
    tdee: Mapped[float | None] = mapped_column(Float)
    target_calories: Mapped[int | None] = mapped_column(Integer)
    target_protein_g: Mapped[int | None] = mapped_column(Integer)
    target_carbs_g: Mapped[int | None] = mapped_column(Integer)
    target_fats_g: Mapped[int | None] = mapped_column(Integer)
    water_target_ml: Mapped[int | None] = mapped_column(Integer)
    plan_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    plan_adjustments: Mapped[list[dict[str, Any]]] = mapped_column(JSONType, default=list)
    last_stall_check_date: Mapped[date | None] = mapped_column(Date)
    stall_weeks: Mapped[int] = mapped_column(Integer, default=0)

    # ── economy ──────────────────────────────────────────────────────────────
    points_balance: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_points_earned: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_points_spent: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    coach_access_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    coach_unlocked_forever: Mapped[bool] = mapped_column(Boolean, default=False)
    subscription_plan: Mapped[str] = mapped_column(String(16), default="none")
    subscription_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    subscription_last_grant_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # ── daily caps (spec §5.11) ──────────────────────────────────────────────
    usage_day: Mapped[date | None] = mapped_column(Date)
    daily_consult_count: Mapped[int] = mapped_column(Integer, default=0)
    daily_coach_messages: Mapped[int] = mapped_column(Integer, default=0)
    daily_vision_count: Mapped[int] = mapped_column(Integer, default=0)
    daily_food_scans_free_used: Mapped[int] = mapped_column(Integer, default=0)
    daily_points_spent: Mapped[int] = mapped_column(Integer, default=0)
    usage_month: Mapped[str | None] = mapped_column(String(7))            # "2026-09"
    monthly_points_spent: Mapped[int] = mapped_column(Integer, default=0)

    # ── gamification ─────────────────────────────────────────────────────────
    streak_current: Mapped[int] = mapped_column(Integer, default=0)
    streak_best: Mapped[int] = mapped_column(Integer, default=0)
    last_activity_day: Mapped[date | None] = mapped_column(Date)
    total_workouts: Mapped[int] = mapped_column(Integer, default=0)
    total_meals_logged: Mapped[int] = mapped_column(Integer, default=0)

    # ── referral ─────────────────────────────────────────────────────────────
    referral_code: Mapped[str] = mapped_column(String(16), unique=True, index=True, nullable=False)
    referred_by_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    referrals_count: Mapped[int] = mapped_column(Integer, default=0)

    # ── moderation & lifecycle ───────────────────────────────────────────────
    is_banned: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    ban_reason: Mapped[str | None] = mapped_column(Text)
    banned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    onboarding_completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    onboarding_stage: Mapped[str | None] = mapped_column(String(32))
    privacy_consent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    privacy_policy_version: Mapped[str | None] = mapped_column(String(16))
    data_deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # ── preferences ──────────────────────────────────────────────────────────
    user_timezone: Mapped[str] = mapped_column(String(64), default="UTC")
    preferences: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    last_active_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    last_seen_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # ── relationships ────────────────────────────────────────────────────────
    daily_logs: Mapped[list[DailyLog]] = relationship(
        back_populates="user", cascade="all, delete-orphan", order_by="DailyLog.log_date"
    )
    meal_entries: Mapped[list[MealEntry]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    weight_records: Mapped[list[WeightRecord]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    measurements: Mapped[list[BodyMeasurement]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    photos: Mapped[list[ProgressPhoto]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    plans: Mapped[list[WorkoutPlan]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    ledger: Mapped[list[PointsLedgerEntry]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    badges: Mapped[list[UserBadge]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    reminders: Mapped[list[ReminderRule]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    # self-referential: who invited me / who did I invite
    referrer: Mapped[User | None] = relationship(
        "User",
        remote_side="User.id",
        foreign_keys="User.referred_by_id",
        back_populates="referred_users",
    )
    referred_users: Mapped[list[User]] = relationship(
        "User",
        foreign_keys="User.referred_by_id",
        back_populates="referrer",
    )

    __table_args__ = (
        Index("ix_users_active", "last_active_at"),
        Index("ix_users_goal_equipment", "goal", "equipment"),
    )

    @property
    def full_name(self) -> str:
        return self.display_name or self.first_name or self.username or f"#{self.tg_id}"

    def local_today(self) -> date:
        """Current calendar date in the user's own timezone (default: UTC)."""
        try:
            from zoneinfo import ZoneInfo

            tz = ZoneInfo(self.user_timezone or "UTC")
        except Exception:  # noqa: BLE001 — bad tz name, fall back
            tz = UTC
        return datetime.now(tz).date()

    def local_now(self) -> datetime:
        try:
            from zoneinfo import ZoneInfo

            tz = ZoneInfo(self.user_timezone or "UTC")
        except Exception:  # noqa: BLE001
            tz = UTC
        return datetime.now(tz)

    @property
    def has_coach_access(self) -> bool:
        if self.coach_unlocked_forever:
            return True
        return bool(as_aware(self.coach_access_until) and as_aware(self.coach_access_until) > utcnow())

    @property
    def onboarding_done(self) -> bool:
        return self.onboarding_completed_at is not None

    @property
    def has_subscription(self) -> bool:
        if self.subscription_plan == "none":
            return False
        return bool(
            as_aware(self.subscription_expires_at) and as_aware(self.subscription_expires_at) > utcnow()
        )


# ═══════════════════════════════════════════════════════════════════════════
#  DAILY TRACKING
# ═══════════════════════════════════════════════════════════════════════════
class DailyLog(Base, TimestampMixin):
    """One row per user per day — the heart of the 24h coach."""

    __tablename__ = "daily_logs"
    __table_args__ = (UniqueConstraint("user_id", "log_date", name="uq_daily_log_user_date"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=new_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    log_date: Mapped[date] = mapped_column(Date, index=True, nullable=False)

    # snapshot of the plan for that day (plans change → history stays truthful)
    target_calories: Mapped[int] = mapped_column(Integer, default=0)
    target_protein_g: Mapped[int] = mapped_column(Integer, default=0)
    target_carbs_g: Mapped[int] = mapped_column(Integer, default=0)
    target_fats_g: Mapped[int] = mapped_column(Integer, default=0)
    water_target_ml: Mapped[int] = mapped_column(Integer, default=0)

    # actuals (kept in sync by the service layer)
    consumed_calories: Mapped[int] = mapped_column(Integer, default=0)
    consumed_protein_g: Mapped[float] = mapped_column(Float, default=0.0)
    consumed_carbs_g: Mapped[float] = mapped_column(Float, default=0.0)
    consumed_fats_g: Mapped[float] = mapped_column(Float, default=0.0)
    water_ml: Mapped[int] = mapped_column(Integer, default=0)
    burned_calories_est: Mapped[int] = mapped_column(Integer, default=0)

    workout_done: Mapped[bool] = mapped_column(Boolean, default=False)
    workout_note: Mapped[str | None] = mapped_column(Text)
    protein_completed: Mapped[bool] = mapped_column(Boolean, default=False)
    meals_planned: Mapped[int] = mapped_column(Integer, default=0)
    meals_logged: Mapped[int] = mapped_column(Integer, default=0)
    skipped_meals: Mapped[list[dict[str, Any]]] = mapped_column(JSONType, default=list)

    steps: Mapped[int | None] = mapped_column(Integer)
    sleep_hours: Mapped[float | None] = mapped_column(Float)
    mood: Mapped[str | None] = mapped_column(String(24))
    soreness: Mapped[int | None] = mapped_column(Integer)          # 1..5
    adherence_score: Mapped[float | None] = mapped_column(Float)    # 0..100
    notes: Mapped[str | None] = mapped_column(Text)
    recalculations: Mapped[list[dict[str, Any]]] = mapped_column(JSONType, default=list)

    user: Mapped[User] = relationship(back_populates="daily_logs")

    @property
    def remaining_calories(self) -> int:
        return int(self.target_calories - self.consumed_calories)

    @property
    def remaining_macros(self) -> dict[str, float]:
        return {
            "protein_g": round(max(0.0, self.target_protein_g - self.consumed_protein_g), 1),
            "carbs_g": round(max(0.0, self.target_carbs_g - self.consumed_carbs_g), 1),
            "fats_g": round(max(0.0, self.target_fats_g - self.consumed_fats_g), 1),
        }


class MealEntry(Base, TimestampMixin):
    """Every logged meal: photo analysis, text description, manual or skipped."""

    __tablename__ = "meal_entries"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=new_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    daily_log_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("daily_logs.id", ondelete="SET NULL"), index=True
    )
    entry_date: Mapped[date] = mapped_column(Date, index=True, default=lambda: utcnow().date())
    kind: Mapped[str] = mapped_column(String(24), default="photo")   # MealKind
    meal_name: Mapped[str | None] = mapped_column(String(255))
    slot: Mapped[str | None] = mapped_column(String(24))             # breakfast|lunch|dinner|snack
    description: Mapped[str | None] = mapped_column(Text)

    calories: Mapped[int] = mapped_column(Integer, default=0)
    protein_g: Mapped[float] = mapped_column(Float, default=0.0)
    carbs_g: Mapped[float] = mapped_column(Float, default=0.0)
    fats_g: Mapped[float] = mapped_column(Float, default=0.0)

    # AI provenance (spec §3.ج + §5.11)
    items: Mapped[list[dict[str, Any]]] = mapped_column(JSONType, default=list)   # parsed ingredients
    confidence: Mapped[float | None] = mapped_column(Float)
    assumptions: Mapped[list[str]] = mapped_column(JSONType, default=list)
    clarifications: Mapped[list[dict[str, Any]]] = mapped_column(JSONType, default=list)
    ai_raw: Mapped[dict[str, Any] | None] = mapped_column(JSONType)
    ai_model: Mapped[str | None] = mapped_column(String(128))
    ai_cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(10, 6))
    points_cost: Mapped[int] = mapped_column(Integer, default=0)

    media_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("media_files.id", ondelete="SET NULL"), index=True
    )
    user: Mapped[User] = relationship(back_populates="meal_entries")

    __table_args__ = (Index("ix_meal_user_date", "user_id", "entry_date"),)


class WeightRecord(Base, TimestampMixin):
    __tablename__ = "weight_records"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=new_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    recorded_on: Mapped[date] = mapped_column(Date, index=True)
    weight_kg: Mapped[float] = mapped_column(Float, nullable=False)
    source: Mapped[str] = mapped_column(String(24), default="manual")
    note: Mapped[str | None] = mapped_column(Text)

    user: Mapped[User] = relationship(back_populates="weight_records")

    __table_args__ = (UniqueConstraint("user_id", "recorded_on", "weight_kg", name="uq_weight_user_day_val"),)


class BodyMeasurement(Base, TimestampMixin):
    """Spec §5.4 — waist/chest/arm… because weight alone misleads."""

    __tablename__ = "body_measurements"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=new_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    recorded_on: Mapped[date] = mapped_column(Date, index=True)
    waist_cm: Mapped[float | None] = mapped_column(Float)
    chest_cm: Mapped[float | None] = mapped_column(Float)
    arm_cm: Mapped[float | None] = mapped_column(Float)
    thigh_cm: Mapped[float | None] = mapped_column(Float)
    hip_cm: Mapped[float | None] = mapped_column(Float)
    neck_cm: Mapped[float | None] = mapped_column(Float)
    calf_cm: Mapped[float | None] = mapped_column(Float)
    shoulder_cm: Mapped[float | None] = mapped_column(Float)
    body_fat_pct: Mapped[float | None] = mapped_column(Float)
    note: Mapped[str | None] = mapped_column(Text)

    user: Mapped[User] = relationship(back_populates="measurements")

    def as_dict(self) -> dict[str, float]:
        return {
            k: v
            for k, v in {
                "waist_cm": self.waist_cm,
                "chest_cm": self.chest_cm,
                "arm_cm": self.arm_cm,
                "thigh_cm": self.thigh_cm,
                "hip_cm": self.hip_cm,
                "neck_cm": self.neck_cm,
                "calf_cm": self.calf_cm,
                "shoulder_cm": self.shoulder_cm,
                "body_fat_pct": self.body_fat_pct,
            }.items()
            if v is not None
        }


class ProgressPhoto(Base, TimestampMixin):
    """Daily/body progress photos — privacy-sensitive (spec §5.12)."""

    __tablename__ = "progress_photos"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=new_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    taken_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    angle: Mapped[str] = mapped_column(String(16), default="front")
    caption: Mapped[str | None] = mapped_column(Text)
    weight_at_time_kg: Mapped[float | None] = mapped_column(Float)

    media_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("media_files.id", ondelete="SET NULL"), index=True
    )
    analysis: Mapped[dict[str, Any] | None] = mapped_column(JSONType)
    compared_with_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("progress_photos.id", ondelete="SET NULL")
    )
    ai_model: Mapped[str | None] = mapped_column(String(128))
    ai_cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(10, 6))
    points_cost: Mapped[int] = mapped_column(Integer, default=0)
    consented: Mapped[bool] = mapped_column(Boolean, default=False)

    user: Mapped[User] = relationship(back_populates="photos")
    media: Mapped[MediaFile | None] = relationship(foreign_keys="ProgressPhoto.media_id")


class MediaFile(Base, TimestampMixin):
    """Physical storage record (S3 key or local path) for any binary asset."""

    __tablename__ = "media_files"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=new_uuid)
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(32), default="photo")   # photo|food|voice|document
    backend: Mapped[str] = mapped_column(String(16), default="local")
    storage_key: Mapped[str] = mapped_column(String(512), nullable=False)
    public_url: Mapped[str | None] = mapped_column(String(1024))
    telegram_file_id: Mapped[str | None] = mapped_column(String(255), index=True)
    mime_type: Mapped[str | None] = mapped_column(String(64))
    size_bytes: Mapped[int | None] = mapped_column(Integer)
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    sha256: Mapped[str | None] = mapped_column(String(64), index=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    purged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    meta: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)


# ═══════════════════════════════════════════════════════════════════════════
#  TRAINING
# ═══════════════════════════════════════════════════════════════════════════
class WorkoutPlan(Base, TimestampMixin):
    """AI-generated programme: weeks → days → exercises (stored as JSON)."""

    __tablename__ = "workout_plans"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=new_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(255))
    kind: Mapped[str] = mapped_column(String(32), default="training")   # training|nutrition|boxing
    goal: Mapped[str | None] = mapped_column(String(32))
    equipment: Mapped[str | None] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16), default="active", index=True)
    weeks: Mapped[list[dict[str, Any]]] = mapped_column(JSONType, default=list)
    schedule: Mapped[list[dict[str, Any]]] = mapped_column(JSONType, default=list)  # day→session map
    nutrition_guide: Mapped[dict[str, Any] | None] = mapped_column(JSONType)
    notes: Mapped[str | None] = mapped_column(Text)
    valid_from: Mapped[date | None] = mapped_column(Date)
    valid_until: Mapped[date | None] = mapped_column(Date)
    ai_model: Mapped[str | None] = mapped_column(String(128))
    ai_cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(10, 6))
    generation_input: Mapped[dict[str, Any] | None] = mapped_column(JSONType)

    user: Mapped[User] = relationship(back_populates="plans")
    sessions: Mapped[list[WorkoutSession]] = relationship(
        back_populates="plan", cascade="all, delete-orphan"
    )


class WorkoutSession(Base, TimestampMixin):
    __tablename__ = "workout_sessions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=new_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    plan_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("workout_plans.id", ondelete="SET NULL"), index=True
    )
    session_date: Mapped[date] = mapped_column(Date, index=True)
    week_index: Mapped[int] = mapped_column(Integer, default=1)
    day_index: Mapped[int] = mapped_column(Integer, default=1)
    title: Mapped[str | None] = mapped_column(String(255))
    focus: Mapped[str | None] = mapped_column(String(64))
    exercises: Mapped[list[dict[str, Any]]] = mapped_column(JSONType, default=list)
    completed: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_min: Mapped[int | None] = mapped_column(Integer)
    calories_burned_est: Mapped[int | None] = mapped_column(Integer)
    rpe: Mapped[int | None] = mapped_column(Integer)          # 1..10 perceived exertion
    feedback: Mapped[str | None] = mapped_column(Text)
    coach_comment: Mapped[str | None] = mapped_column(Text)

    user: Mapped[User] = relationship()
    plan: Mapped[WorkoutPlan | None] = relationship(back_populates="sessions")


# ═══════════════════════════════════════════════════════════════════════════
#  CONVERSATION / AI
# ═══════════════════════════════════════════════════════════════════════════
class ChatMessage(Base, TimestampMixin):
    """Rolling conversation memory, split per scope (consult vs. coach)."""

    __tablename__ = "chat_messages"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=new_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    scope: Mapped[str] = mapped_column(String(16), default="coach")   # coach|consult|admin
    role: Mapped[str] = mapped_column(String(16), nullable=False)     # system|user|assistant
    content: Mapped[str] = mapped_column(Text, nullable=False)
    tokens: Mapped[int | None] = mapped_column(Integer)
    model: Mapped[str | None] = mapped_column(String(128))
    has_media: Mapped[bool] = mapped_column(Boolean, default=False)
    summarized_into: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("chat_messages.id", ondelete="SET NULL")
    )
    is_summary: Mapped[bool] = mapped_column(Boolean, default=False)

    __table_args__ = (Index("ix_chat_user_scope_created", "user_id", "scope", "created_at"),)


class AIUsage(Base):
    """Per-call API telemetry → admin cost dashboard + per-user caps."""

    __tablename__ = "ai_usage"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=new_uuid)
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    task: Mapped[str] = mapped_column(String(24), index=True)
    model: Mapped[str] = mapped_column(String(128), index=True)
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6), default=Decimal("0"))
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(24), default="ok")     # ok|error|fallback|capped
    fallback_from: Mapped[str | None] = mapped_column(String(128))
    error: Mapped[str | None] = mapped_column(Text)
    points_charged: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, server_default=func.now(), index=True
    )

    __table_args__ = (Index("ix_ai_usage_task_created", "task", "created_at"),)


class FoodItem(Base, TimestampMixin):
    """Personal food database — learn the user's frequent meals to answer
    faster and cheaper next time (no AI call needed)."""

    __tablename__ = "food_items"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=new_uuid)
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    name_normalized: Mapped[str] = mapped_column(String(255), index=True, nullable=False)
    portion: Mapped[str] = mapped_column(String(64), default="100g")
    calories_per_portion: Mapped[int] = mapped_column(Integer, default=0)
    protein_g: Mapped[float] = mapped_column(Float, default=0.0)
    carbs_g: Mapped[float] = mapped_column(Float, default=0.0)
    fats_g: Mapped[float] = mapped_column(Float, default=0.0)
    is_global: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    cuisine: Mapped[str | None] = mapped_column(String(64))
    times_used: Mapped[int] = mapped_column(Integer, default=0)
    source: Mapped[str] = mapped_column(String(24), default="ai")
    meta: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)

    __table_args__ = (
        UniqueConstraint("user_id", "name_normalized", "portion", name="uq_food_item_user_name"),
    )


class ExerciseVideo(Base, TimestampMixin):
    """Cache of YouTube links per exercise (spec §3.أ — every exercise needs a
    video link).  Avoids burning API quota on repeated searches."""

    __tablename__ = "exercise_videos"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=new_uuid)
    query_key: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    language: Mapped[str] = mapped_column(String(8), default="ar")
    exercise_name: Mapped[str] = mapped_column(String(255))
    video_url: Mapped[str | None] = mapped_column(String(512))
    video_title: Mapped[str | None] = mapped_column(String(512))
    channel: Mapped[str | None] = mapped_column(String(255))
    source: Mapped[str] = mapped_column(String(24), default="youtube_api")  # youtube_api|search_url|ai
    hits: Mapped[int] = mapped_column(Integer, default=0)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# ═══════════════════════════════════════════════════════════════════════════
#  ECONOMY / GAMIFICATION
# ═══════════════════════════════════════════════════════════════════════════
class PointsLedgerEntry(Base):
    """Append-only ledger — the audit trail for every point movement."""

    __tablename__ = "points_ledger"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=new_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    amount: Mapped[int] = mapped_column(Integer, nullable=False)      # + credit / − debit
    balance_after: Mapped[int] = mapped_column(Integer, nullable=False)
    reason: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    note: Mapped[str | None] = mapped_column(Text)
    reference: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    actor_tg_id: Mapped[int | None] = mapped_column(BigInteger)       # admin who did it
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, server_default=func.now(), index=True
    )

    user: Mapped[User] = relationship(back_populates="ledger")

    __table_args__ = (Index("ix_ledger_user_created", "user_id", "created_at"),)


class UserBadge(Base):
    __tablename__ = "user_badges"
    __table_args__ = (UniqueConstraint("user_id", "badge_key", name="uq_user_badge"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=new_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    badge_key: Mapped[str] = mapped_column(String(48), nullable=False)
    earned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    meta: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)

    user: Mapped[User] = relationship(back_populates="badges")


class ReferralEvent(Base):
    __tablename__ = "referral_events"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=new_uuid)
    inviter_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    invitee_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    code: Mapped[str] = mapped_column(String(16), index=True)
    inviter_points: Mapped[int] = mapped_column(Integer, default=0)
    invitee_points: Mapped[int] = mapped_column(Integer, default=0)
    qualified: Mapped[bool] = mapped_column(Boolean, default=False)   # invitee finished onboarding
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, server_default=func.now()
    )

    __table_args__ = (UniqueConstraint("invitee_id", name="uq_referral_invitee"),)


class ReminderRule(Base, TimestampMixin):
    """Per-user reminder configuration (spec §5.1)."""

    __tablename__ = "reminder_rules"
    __table_args__ = (UniqueConstraint("user_id", "kind", name="uq_reminder_user_kind"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=new_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)     # ReminderKind
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    hour: Mapped[int | None] = mapped_column(Integer)
    minute: Mapped[int] = mapped_column(Integer, default=0)
    days_of_week: Mapped[list[int]] = mapped_column(JSONType, default=list)   # 0=Mon
    interval_minutes: Mapped[int | None] = mapped_column(Integer)
    last_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)

    user: Mapped[User] = relationship(back_populates="reminders")


# ═══════════════════════════════════════════════════════════════════════════
#  ADMIN / OPERATIONS
# ═══════════════════════════════════════════════════════════════════════════
class Broadcast(Base, TimestampMixin):
    __tablename__ = "broadcasts"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=new_uuid)
    admin_tg_id: Mapped[int | None] = mapped_column(BigInteger)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    media_file_id: Mapped[str | None] = mapped_column(String(255))
    audience: Mapped[str] = mapped_column(String(32), default="all")  # all|active|coaches|banned_free
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    scheduled_for: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    total: Mapped[int] = mapped_column(Integer, default=0)
    sent: Mapped[int] = mapped_column(Integer, default=0)
    failed: Mapped[int] = mapped_column(Integer, default=0)
    errors: Mapped[list[dict[str, Any]]] = mapped_column(JSONType, default=list)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AdminAction(Base):
    """Audit log for every privileged operation."""

    __tablename__ = "admin_actions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=new_uuid)
    admin_tg_id: Mapped[int] = mapped_column(BigInteger, index=True)
    action: Mapped[str] = mapped_column(String(48), index=True)
    target_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    payload: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, server_default=func.now(), index=True
    )


class AppSetting(Base, TimestampMixin):
    """Key/value runtime config editable from the admin panel without redeploy
    (model routing, point prices, daily caps, forced channels, broadcasts…)."""

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(96), primary_key=True)
    value: Mapped[dict[str, Any] | list[Any] | Any] = mapped_column(JSONType, default=dict)
    description: Mapped[str | None] = mapped_column(Text)
    updated_by: Mapped[int | None] = mapped_column(BigInteger)


class ConsentRecord(Base):
    """Privacy consent history (spec §5.12)."""

    __tablename__ = "consent_records"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=new_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(32), default="privacy_policy")
    version: Mapped[str] = mapped_column(String(16), default="1.0")
    granted: Mapped[bool] = mapped_column(Boolean, default=True)
    ip_hint: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, server_default=func.now()
    )


ALL_MODELS: tuple[type[Base], ...] = (
    User,
    DailyLog,
    MealEntry,
    WeightRecord,
    BodyMeasurement,
    ProgressPhoto,
    MediaFile,
    WorkoutPlan,
    WorkoutSession,
    ChatMessage,
    AIUsage,
    FoodItem,
    ExerciseVideo,
    PointsLedgerEntry,
    UserBadge,
    ReferralEvent,
    ReminderRule,
    Broadcast,
    AdminAction,
    AppSetting,
    ConsentRecord,
)
