"""Daily tracking data access: logs, meals, weight, measurements, photos."""
from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import and_, delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.models import (
    BodyMeasurement,
    DailyLog,
    MealEntry,
    MediaFile,
    ProgressPhoto,
    User,
    WeightRecord,
    utcnow,
)


class TrackingRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.s = session

    # ── daily log ────────────────────────────────────────────────────────────
    async def daily_log(self, user: User, day: date | None = None) -> DailyLog:
        """Fetch (or create) today's log, re-syncing the targets from the plan."""
        day = day or user.local_today()
        log = (
            await self.s.execute(
                select(DailyLog).where(DailyLog.user_id == user.id, DailyLog.log_date == day)
            )
        ).scalar_one_or_none()
        if log is None:
            log = DailyLog(user_id=user.id, log_date=day)
            self.s.add(log)
        # keep the day's targets aligned with the *current* plan
        log.target_calories = int(user.target_calories or 0)
        log.target_protein_g = int(user.target_protein_g or 0)
        log.target_carbs_g = int(user.target_carbs_g or 0)
        log.target_fats_g = int(user.target_fats_g or 0)
        log.water_target_ml = int(user.water_target_ml or 0)
        await self.s.flush()
        return log

    async def log_for(self, user_id: uuid.UUID, day: date) -> DailyLog | None:
        return (
            await self.s.execute(
                select(DailyLog).where(DailyLog.user_id == user_id, DailyLog.log_date == day)
            )
        ).scalar_one_or_none()

    async def logs_between(self, user_id: uuid.UUID, start: date, end: date) -> Sequence[DailyLog]:
        return (
            await self.s.execute(
                select(DailyLog)
                .where(DailyLog.user_id == user_id, DailyLog.log_date.between(start, end))
                .order_by(DailyLog.log_date)
            )
        ).scalars().all()

    async def last_logs(self, user_id: uuid.UUID, days: int = 30) -> Sequence[DailyLog]:
        return (
            await self.s.execute(
                select(DailyLog)
                .where(DailyLog.user_id == user_id)
                .order_by(DailyLog.log_date.desc())
                .limit(days)
            )
        ).scalars().all()

    async def recompute_day(self, log: DailyLog) -> DailyLog:
        """Re-derive consumed macros from the meal rows (single source of truth)."""
        rows = (
            await self.s.execute(select(MealEntry).where(MealEntry.daily_log_id == log.id))
        ).scalars().all()
        log.consumed_calories = int(sum(m.calories or 0 for m in rows if m.kind != "skipped"))
        log.consumed_protein_g = round(sum(float(m.protein_g or 0) for m in rows if m.kind != "skipped"), 1)
        log.consumed_carbs_g = round(sum(float(m.carbs_g or 0) for m in rows if m.kind != "skipped"), 1)
        log.consumed_fats_g = round(sum(float(m.fats_g or 0) for m in rows if m.kind != "skipped"), 1)
        log.meals_logged = len([m for m in rows if m.kind != "skipped"])
        log.protein_completed = (
            log.target_protein_g > 0 and log.consumed_protein_g >= 0.9 * log.target_protein_g
        )
        log.adherence_score = round(_adherence(log), 1)
        await self.s.flush()
        return log

    # ── meals ────────────────────────────────────────────────────────────────
    async def add_meal(self, user: User, day: date, **data: Any) -> MealEntry:
        log = await self.daily_log(user, day)
        meal = MealEntry(user_id=user.id, daily_log_id=log.id, entry_date=day, **data)
        self.s.add(meal)
        await self.s.flush()
        await self.recompute_day(log)
        user.total_meals_logged = (user.total_meals_logged or 0) + 1
        await self.s.flush()
        return meal

    async def meal_by_id(self, meal_id: uuid.UUID | str) -> MealEntry | None:
        if isinstance(meal_id, str):
            meal_id = uuid.UUID(meal_id)
        return await self.s.get(MealEntry, meal_id)

    async def meals_of_day(self, user_id: uuid.UUID, day: date) -> Sequence[MealEntry]:
        return (
            await self.s.execute(
                select(MealEntry)
                .where(MealEntry.user_id == user_id, MealEntry.entry_date == day)
                .order_by(MealEntry.created_at)
            )
        ).scalars().all()

    async def meals_between(self, user_id: uuid.UUID, start: date, end: date) -> Sequence[MealEntry]:
        return (
            await self.s.execute(
                select(MealEntry)
                .where(MealEntry.user_id == user_id, MealEntry.entry_date.between(start, end))
                .order_by(MealEntry.entry_date)
            )
        ).scalars().all()

    async def delete_meal(self, meal: MealEntry) -> None:
        log_id = meal.daily_log_id
        user_id = meal.user_id
        await self.s.delete(meal)
        await self.s.flush()
        if log_id:
            log = await self.s.get(DailyLog, log_id)
            if log:
                await self.recompute_day(log)
        user = await self.s.get(User, user_id)
        if user and user.total_meals_logged:
            user.total_meals_logged -= 1
            await self.s.flush()

    async def add_water(self, user: User, day: date, ml: int) -> DailyLog:
        log = await self.daily_log(user, day)
        log.water_ml = int(log.water_ml or 0) + int(ml)
        await self.s.flush()
        return log

    async def log_skipped(self, user: User, *, day: date | None = None, slot: str | None = None,
                          reason: str | None = None) -> DailyLog:
        """Register a missed meal and recompute the day immediately (spec §3.ب)."""
        day = day or user.local_today()
        log = await self.daily_log(user, day)
        skipped = list(log.skipped_meals or [])
        skipped.append({"slot": slot, "reason": (reason or "")[:300], "at": utcnow().isoformat()})
        log.skipped_meals = skipped
        log.meals_planned = max(int(log.meals_planned or 0), len(skipped) + int(log.meals_logged or 0))
        await self.recompute_day(log)
        await self.s.flush()
        return log

    async def set_workout_done(
        self, user: User, *, day: date | None = None, calories: int = 0, note: str | None = None
    ) -> DailyLog:
        day = day or user.local_today()
        log = await self.daily_log(user, day)
        if not log.workout_done:
            user.total_workouts = int(user.total_workouts or 0) + 1
        log.workout_done = True
        log.burned_calories_est = int(log.burned_calories_est or 0) + int(calories or 0)
        if note:
            log.workout_note = note[:500]
        await self.recompute_day(log)
        await self.s.flush()
        return log

    async def set_sleep_mood(
        self, user: User, *, day: date | None = None, sleep_hours: float | None = None,
        mood: str | None = None, soreness: int | None = None, steps: int | None = None
    ) -> DailyLog:
        day = day or user.local_today()
        log = await self.daily_log(user, day)
        if sleep_hours is not None:
            log.sleep_hours = sleep_hours
        if mood:
            log.mood = mood[:24]
        if soreness is not None:
            log.soreness = max(1, min(5, int(soreness)))
        if steps is not None:
            log.steps = int(steps)
        await self.recompute_day(log)
        return log

    # ── weight ───────────────────────────────────────────────────────────────
    async def record_weight(
        self, user: User, weight_kg: float, day: date | None = None, note: str | None = None
    ) -> WeightRecord:
        day = day or user.local_today()
        existing = (
            await self.s.execute(
                select(WeightRecord).where(
                    WeightRecord.user_id == user.id, WeightRecord.recorded_on == day
                )
            )
        ).scalar_one_or_none()
        if existing:
            existing.weight_kg = float(weight_kg)
            existing.note = note or existing.note
            rec = existing
        else:
            rec = WeightRecord(
                user_id=user.id, recorded_on=day, weight_kg=float(weight_kg), note=note
            )
            self.s.add(rec)
        user.weight_kg = float(weight_kg)
        await self.s.flush()
        return rec

    async def weight_history(
        self, user_id: uuid.UUID, days: int | None = None, limit: int = 400
    ) -> Sequence[WeightRecord]:
        stmt = select(WeightRecord).where(WeightRecord.user_id == user_id).order_by(
            WeightRecord.recorded_on
        )
        if days:
            stmt = stmt.where(WeightRecord.recorded_on >= utcnow().date() - timedelta(days=days))
        return (await self.s.execute(stmt.limit(limit))).scalars().all()

    async def latest_weight(self, user_id: uuid.UUID) -> WeightRecord | None:
        return (
            await self.s.execute(
                select(WeightRecord)
                .where(WeightRecord.user_id == user_id)
                .order_by(WeightRecord.recorded_on.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

    async def weight_trend(self, user_id: uuid.UUID, weeks: int = 2) -> dict[str, Any]:
        """Average weekly delta used by the automatic calorie re-calibration."""
        since = utcnow().date() - timedelta(weeks=weeks)
        rows = (
            await self.s.execute(
                select(WeightRecord)
                .where(WeightRecord.user_id == user_id, WeightRecord.recorded_on >= since)
                .order_by(WeightRecord.recorded_on)
            )
        ).scalars().all()
        if len(rows) < 2:
            return {"samples": len(rows), "delta_kg": 0.0, "per_week_kg": 0.0, "stalled": False}
        first, last = float(rows[0].weight_kg), float(rows[-1].weight_kg)
        span_days = max(1, (rows[-1].recorded_on - rows[0].recorded_on).days)
        per_week = (last - first) / span_days * 7
        return {
            "samples": len(rows),
            "delta_kg": round(last - first, 2),
            "per_week_kg": round(per_week, 2),
            "stalled": abs(per_week) < 0.15,
            "first_kg": first,
            "last_kg": last,
            "span_days": span_days,
        }

    # ── measurements ─────────────────────────────────────────────────────────
    async def record_measurements(
        self, user: User, values: dict[str, float], day: date | None = None, note: str | None = None
    ) -> BodyMeasurement:
        day = day or user.local_today()
        allowed = {
            "waist_cm", "chest_cm", "arm_cm", "thigh_cm", "hip_cm",
            "neck_cm", "calf_cm", "shoulder_cm", "body_fat_pct",
        }
        payload = {k: float(v) for k, v in values.items() if k in allowed and v is not None}
        existing = (
            await self.s.execute(
                select(BodyMeasurement).where(
                    BodyMeasurement.user_id == user.id, BodyMeasurement.recorded_on == day
                )
            )
        ).scalar_one_or_none()
        if existing:
            for k, v in payload.items():
                setattr(existing, k, v)
            existing.note = note or existing.note
            rec = existing
        else:
            rec = BodyMeasurement(user_id=user.id, recorded_on=day, note=note, **payload)
            self.s.add(rec)
        await self.s.flush()
        return rec

    async def measurement_history(
        self, user_id: uuid.UUID, limit: int = 60
    ) -> Sequence[BodyMeasurement]:
        return (
            await self.s.execute(
                select(BodyMeasurement)
                .where(BodyMeasurement.user_id == user_id)
                .order_by(BodyMeasurement.recorded_on.desc())
                .limit(limit)
            )
        ).scalars().all()

    # ── progress photos ──────────────────────────────────────────────────────
    async def add_photo(
        self,
        user: User,
        *,
        media_id: uuid.UUID | None,
        angle: str = "front",
        caption: str | None = None,
        consented: bool = False,
    ) -> ProgressPhoto:
        photo = ProgressPhoto(
            user_id=user.id,
            taken_at=utcnow(),
            angle=angle,
            caption=caption,
            weight_at_time_kg=user.weight_kg,
            media_id=media_id,
            consented=consented,
        )
        self.s.add(photo)
        await self.s.flush()
        return photo

    async def photos(
        self, user_id: uuid.UUID, angle: str | None = None, limit: int = 40
    ) -> Sequence[ProgressPhoto]:
        stmt = (
            select(ProgressPhoto)
            .options(selectinload(ProgressPhoto.media))
            .where(ProgressPhoto.user_id == user_id)
        )
        if angle:
            stmt = stmt.where(ProgressPhoto.angle == angle)
        return (
            await self.s.execute(stmt.order_by(ProgressPhoto.taken_at.desc()).limit(limit))
        ).scalars().all()

    async def photo_by_id(self, photo_id: uuid.UUID | str) -> ProgressPhoto | None:
        if isinstance(photo_id, str):
            photo_id = uuid.UUID(photo_id)
        return await self.s.get(ProgressPhoto, photo_id)

    async def photo_pair_for_comparison(
        self, user_id: uuid.UUID, angle: str | None = None
    ) -> tuple[ProgressPhoto, ProgressPhoto] | None:
        """Newest + oldest photo (optionally of the same angle) for before/after."""
        rows = list(await self.photos(user_id, angle=angle, limit=200))
        if len(rows) < 2:
            return None
        newest = rows[0]
        oldest = next((p for p in reversed(rows) if p.id != newest.id), None)
        return (newest, oldest) if oldest else None

    async def save_photo_analysis(
        self,
        photo: ProgressPhoto,
        analysis: dict[str, Any],
        model: str | None = None,
        cost_usd: float | None = None,
        points: int = 0,
    ) -> ProgressPhoto:
        photo.analysis = analysis
        photo.ai_model = model
        photo.ai_cost_usd = cost_usd
        photo.points_cost = points
        await self.s.flush()
        return photo

    async def delete_user_photos(self, user_id: uuid.UUID) -> int:
        media_ids = [
            m
            for m in (
                await self.s.execute(
                    select(ProgressPhoto.media_id).where(ProgressPhoto.user_id == user_id)
                )
            ).scalars().all()
            if m
        ]
        res = await self.s.execute(delete(ProgressPhoto).where(ProgressPhoto.user_id == user_id))
        if media_ids:
            await self.s.execute(
                delete(MediaFile).where(and_(MediaFile.id.in_(media_ids)))
            )
        return int(res.rowcount or 0)

    # ── aggregate helpers ────────────────────────────────────────────────────
    async def summary_between(self, user_id: uuid.UUID, start: date, end: date) -> dict[str, Any]:
        logs = await self.logs_between(user_id, start, end)
        if not logs:
            return {"days": 0, "avg_calories": 0, "avg_protein": 0.0, "workouts": 0, "adherence": 0.0}
        n = len(logs)
        return {
            "days": n,
            "avg_calories": round(sum(int(row.consumed_calories or 0) for row in logs) / n),
            "avg_target": round(sum(int(row.target_calories or 0) for row in logs) / n),
            "avg_protein": round(sum(float(row.consumed_protein_g or 0) for row in logs) / n, 1),
            "avg_water_ml": round(sum(int(row.water_ml or 0) for row in logs) / n),
            "workouts": sum(1 for row in logs if row.workout_done),
            "adherence": round(
                sum(float(row.adherence_score or 0) for row in logs) / max(1, sum(1 for row in logs if row.adherence_score is not None)),
                1,
            ),
            "total_calories": sum(int(row.consumed_calories or 0) for row in logs),
        }

    async def days_since_last_weigh_in(self, user_id: uuid.UUID) -> int | None:
        latest = await self.latest_weight(user_id)
        if not latest:
            return None
        return (utcnow().date() - latest.recorded_on).days

    async def streak_state(self, user_id: uuid.UUID, days: int = 60) -> dict[str, Any]:
        """Recompute streak from the logs (adherence ≥ 60% counts as a good day)."""
        rows = (
            await self.s.execute(
                select(DailyLog).where(DailyLog.user_id == user_id).order_by(DailyLog.log_date.desc()).limit(days)
            )
        ).scalars().all()
        good = {r.log_date for r in rows if _is_good_day(r)}
        streak = 0
        cursor = utcnow().date()
        if cursor not in good:
            cursor -= timedelta(days=1)  # today not logged yet → don't break the streak
        while cursor in good:
            streak += 1
            cursor -= timedelta(days=1)
        return {"current": streak, "good_days": len(good)}


def _adherence(log: DailyLog) -> float:
    """0-100 score blending calorie accuracy, protein, water and training."""
    score = 0.0
    if log.target_calories:
        ratio = log.consumed_calories / log.target_calories
        score += 40 * max(0.0, 1 - abs(1 - ratio) * 2.2)
    if log.target_protein_g:
        score += 25 * min(1.0, log.consumed_protein_g / log.target_protein_g)
    if log.water_target_ml:
        score += 15 * min(1.0, (log.water_ml or 0) / log.water_target_ml)
    if log.workout_done:
        score += 15
    if log.meals_logged:
        score += 5
    return max(0.0, min(100.0, score))


def _is_good_day(log: DailyLog) -> bool:
    return (log.adherence_score or 0) >= 60 or log.workout_done


def now() -> datetime:  # small helper used by callers importing from here
    return utcnow()
