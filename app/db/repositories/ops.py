"""Operational data access: AI telemetry, media, broadcasts, audit log, runtime settings."""
from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    AdminAction,
    AIUsage,
    AppSetting,
    Broadcast,
    ExerciseVideo,
    FoodItem,
    MediaFile,
    User,
    as_aware,
    utcnow,
)


class AIUsageRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.s = session

    async def log(
        self,
        *,
        user_id: uuid.UUID | None,
        task: str,
        model: str,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        cost_usd: float = 0.0,
        latency_ms: int | None = None,
        status: str = "ok",
        fallback_from: str | None = None,
        error: str | None = None,
        points_charged: int = 0,
    ) -> AIUsage:
        row = AIUsage(
            user_id=user_id,
            task=task,
            model=model,
            prompt_tokens=int(prompt_tokens or 0),
            completion_tokens=int(completion_tokens or 0),
            total_tokens=int((prompt_tokens or 0) + (completion_tokens or 0)),
            cost_usd=Decimal(str(round(float(cost_usd or 0.0), 6))),
            latency_ms=latency_ms,
            status=status,
            fallback_from=fallback_from,
            error=(error or None) and str(error)[:2000],
            points_charged=int(points_charged or 0),
        )
        self.s.add(row)
        await self.s.flush()
        return row

    async def cost_since(self, since: datetime, user_id: uuid.UUID | None = None) -> Decimal:
        stmt = select(func.coalesce(func.sum(AIUsage.cost_usd), 0)).where(
            AIUsage.created_at >= since, AIUsage.status != "error"
        )
        if user_id:
            stmt = stmt.where(AIUsage.user_id == user_id)
        value = (await self.s.execute(stmt)).scalar_one()
        return Decimal(str(value))

    async def today_cost(self, user_id: uuid.UUID | None = None) -> Decimal:
        start = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        return await self.cost_since(start, user_id)

    async def breakdown(self, days: int = 7) -> dict[str, Any]:
        since = utcnow() - timedelta(days=days)
        by_task = (
            await self.s.execute(
                select(AIUsage.task, func.count(AIUsage.id), func.sum(AIUsage.cost_usd), func.sum(AIUsage.total_tokens))
                .where(AIUsage.created_at >= since)
                .group_by(AIUsage.task)
            )
        ).all()
        by_model = (
            await self.s.execute(
                select(AIUsage.model, func.count(AIUsage.id), func.sum(AIUsage.cost_usd), func.sum(AIUsage.total_tokens))
                .where(AIUsage.created_at >= since)
                .group_by(AIUsage.model)
            )
        ).all()
        by_day = (
            await self.s.execute(
                select(func.date(AIUsage.created_at), func.count(AIUsage.id), func.sum(AIUsage.cost_usd))
                .where(AIUsage.created_at >= since)
                .group_by(func.date(AIUsage.created_at))
                .order_by(func.date(AIUsage.created_at))
            )
        ).all()
        errors = (
            await self.s.execute(
                select(func.count(AIUsage.id)).where(
                    AIUsage.created_at >= since, AIUsage.status != "ok"
                )
            )
        ).scalar_one()
        return {
            "days": days,
            "total_cost_usd": float(await self.cost_since(since)),
            "calls": int(sum(r[1] for r in by_task)),
            "errors": int(errors),
            "by_task": [
                {"task": r[0], "calls": int(r[1]), "cost_usd": float(r[2] or 0), "tokens": int(r[3] or 0)}
                for r in by_task
            ],
            "by_model": [
                {"model": r[0], "calls": int(r[1]), "cost_usd": float(r[2] or 0), "tokens": int(r[3] or 0)}
                for r in by_model
            ],
            "by_day": [
                {"date": str(r[0]), "calls": int(r[1]), "cost_usd": float(r[2] or 0)} for r in by_day
            ],
        }

    async def user_cost(self, user_id: uuid.UUID, days: int = 30) -> dict[str, Any]:
        since = utcnow() - timedelta(days=days)
        row = (
            await self.s.execute(
                select(
                    func.count(AIUsage.id),
                    func.coalesce(func.sum(AIUsage.cost_usd), 0),
                    func.coalesce(func.sum(AIUsage.total_tokens), 0),
                ).where(AIUsage.user_id == user_id, AIUsage.created_at >= since)
            )
        ).one()
        return {"calls": int(row[0]), "cost_usd": float(row[1]), "tokens": int(row[2])}

    async def recent(self, limit: int = 25) -> Sequence[AIUsage]:
        return (
            await self.s.execute(select(AIUsage).order_by(AIUsage.created_at.desc()).limit(limit))
        ).scalars().all()


class MediaRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.s = session

    async def register(self, user_id: uuid.UUID | None, **data: Any) -> MediaFile:
        media = MediaFile(user_id=user_id, **data)
        self.s.add(media)
        await self.s.flush()
        return media

    async def by_id(self, media_id: uuid.UUID | str) -> MediaFile | None:
        if isinstance(media_id, str):
            media_id = uuid.UUID(media_id)
        return await self.s.get(MediaFile, media_id)

    async def by_telegram_file_id(self, file_id: str) -> MediaFile | None:
        return (
            await self.s.execute(select(MediaFile).where(MediaFile.telegram_file_id == file_id).limit(1))
        ).scalar_one_or_none()

    async def user_media(self, user_id: uuid.UUID, limit: int = 50) -> Sequence[MediaFile]:
        return (
            await self.s.execute(
                select(MediaFile)
                .where(MediaFile.user_id == user_id)
                .order_by(MediaFile.created_at.desc())
                .limit(limit)
            )
        ).scalars().all()

    async def mark_purged(self, media: MediaFile) -> None:
        media.purged_at = utcnow()
        await self.s.flush()

    async def expired(self, limit: int = 500) -> Sequence[MediaFile]:
        return (
            await self.s.execute(
                select(MediaFile)
                .where(
                    MediaFile.purged_at.is_(None),
                    MediaFile.expires_at.is_not(None),
                    MediaFile.expires_at <= utcnow(),
                )
                .limit(limit)
            )
        ).scalars().all()

    async def delete_for_user(self, user_id: uuid.UUID) -> int:
        res = await self.s.execute(delete(MediaFile).where(MediaFile.user_id == user_id))
        return int(res.rowcount or 0)


class BroadcastRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.s = session

    async def create(self, admin_tg_id: int | None, **data: Any) -> Broadcast:
        bc = Broadcast(admin_tg_id=admin_tg_id, **data)
        self.s.add(bc)
        await self.s.flush()
        return bc

    async def by_id(self, broadcast_id: uuid.UUID | str) -> Broadcast | None:
        if isinstance(broadcast_id, str):
            broadcast_id = uuid.UUID(broadcast_id)
        return await self.s.get(Broadcast, broadcast_id)

    async def pending(self) -> Sequence[Broadcast]:
        now = utcnow()
        return (
            await self.s.execute(
                select(Broadcast).where(
                    Broadcast.status.in_(["pending", "running"]),
                    (Broadcast.scheduled_for.is_(None)) | (Broadcast.scheduled_for <= now),
                )
            )
        ).scalars().all()

    async def progress(self, bc: Broadcast, sent: int, failed: int) -> None:
        bc.sent = sent
        bc.failed = failed
        await self.s.flush()

    async def finish(self, bc: Broadcast, status: str = "done") -> None:
        bc.status = status
        bc.finished_at = utcnow()
        await self.s.flush()

    async def history(self, limit: int = 20) -> Sequence[Broadcast]:
        return (
            await self.s.execute(select(Broadcast).order_by(Broadcast.created_at.desc()).limit(limit))
        ).scalars().all()


class AuditRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.s = session

    async def log(
        self,
        admin_tg_id: int,
        action: str,
        *,
        target_user_id: uuid.UUID | None = None,
        payload: dict[str, Any] | None = None,
    ) -> AdminAction:
        row = AdminAction(
            admin_tg_id=admin_tg_id,
            action=getattr(action, "value", action),
            target_user_id=target_user_id,
            payload=payload or {},
        )
        self.s.add(row)
        await self.s.flush()
        return row

    async def recent(self, limit: int = 30) -> Sequence[AdminAction]:
        return (
            await self.s.execute(
                select(AdminAction).order_by(AdminAction.created_at.desc()).limit(limit)
            )
        ).scalars().all()

    async def for_user(self, user_id: uuid.UUID, limit: int = 20) -> Sequence[AdminAction]:
        return (
            await self.s.execute(
                select(AdminAction)
                .where(AdminAction.target_user_id == user_id)
                .order_by(AdminAction.created_at.desc())
                .limit(limit)
            )
        ).scalars().all()


class SettingsRepository:
    """Live runtime overrides stored in ``app_settings`` (admin-editable)."""

    def __init__(self, session: AsyncSession) -> None:
        self.s = session

    async def get(self, key: str, default: Any = None) -> Any:
        row = await self.s.get(AppSetting, key)
        return row.value if row else default

    async def set(
        self, key: str, value: Any, *, description: str | None = None, updated_by: int | None = None
    ) -> AppSetting:
        row = await self.s.get(AppSetting, key)
        if row is None:
            row = AppSetting(key=key, value=value, description=description, updated_by=updated_by)
            self.s.add(row)
        else:
            row.value = value
            row.updated_by = updated_by
            if description:
                row.description = description
        await self.s.flush()
        return row

    async def all(self) -> dict[str, Any]:
        rows = (await self.s.execute(select(AppSetting))).scalars().all()
        return {r.key: r.value for r in rows}

    async def delete(self, key: str) -> None:
        await self.s.execute(delete(AppSetting).where(AppSetting.key == key))


class CacheRepository:
    """YouTube link cache + personal/global food database."""

    def __init__(self, session: AsyncSession) -> None:
        self.s = session

    # ── exercise videos ──────────────────────────────────────────────────────
    async def video(self, query_key: str, language: str = "ar") -> ExerciseVideo | None:
        row = (
            await self.s.execute(
                select(ExerciseVideo).where(
                    ExerciseVideo.query_key == query_key, ExerciseVideo.language == language
                )
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        row.hits = (row.hits or 0) + 1
        await self.s.flush()
        if row.expires_at and as_aware(row.expires_at) < utcnow():
            return None
        return row

    async def put_video(self, query_key: str, language: str, **data: Any) -> ExerciseVideo:
        existing = (
            await self.s.execute(
                select(ExerciseVideo).where(
                    ExerciseVideo.query_key == query_key, ExerciseVideo.language == language
                )
            )
        ).scalar_one_or_none()
        if existing:
            for k, v in data.items():
                setattr(existing, k, v)
            existing.expires_at = utcnow() + timedelta(days=data.pop("cache_days", 30))
            await self.s.flush()
            return existing
        row = ExerciseVideo(
            query_key=query_key,
            language=language,
            expires_at=utcnow() + timedelta(days=data.pop("cache_days", 30)),
            **data,
        )
        self.s.add(row)
        await self.s.flush()
        return row

    # ── food items ───────────────────────────────────────────────────────────
    async def find_food(
        self, name_normalized: str, user_id: uuid.UUID | None = None
    ) -> FoodItem | None:
        stmt = select(FoodItem).where(FoodItem.name_normalized == name_normalized)
        if user_id:
            stmt = stmt.where((FoodItem.user_id == user_id) | (FoodItem.is_global.is_(True)))
        else:
            stmt = stmt.where(FoodItem.is_global.is_(True))
        row = (await self.s.execute(stmt.order_by(FoodItem.times_used.desc()).limit(1))).scalar_one_or_none()
        if row:
            row.times_used = (row.times_used or 0) + 1
            await self.s.flush()
        return row

    async def save_food(self, user_id: uuid.UUID | None, **data: Any) -> FoodItem:
        existing = (
            await self.s.execute(
                select(FoodItem).where(
                    FoodItem.name_normalized == data.get("name_normalized"),
                    FoodItem.portion == data.get("portion", "100g"),
                    (FoodItem.user_id == user_id) if user_id else FoodItem.is_global.is_(True),
                )
            )
        ).scalar_one_or_none()
        if existing:
            for k, v in data.items():
                setattr(existing, k, v)
            await self.s.flush()
            return existing
        row = FoodItem(user_id=user_id, **data)
        self.s.add(row)
        await self.s.flush()
        return row

    async def user_foods(self, user_id: uuid.UUID, limit: int = 40) -> Sequence[FoodItem]:
        return (
            await self.s.execute(
                select(FoodItem)
                .where(FoodItem.user_id == user_id)
                .order_by(FoodItem.times_used.desc())
                .limit(limit)
            )
        ).scalars().all()


async def count_users_created_between(session: AsyncSession, start: datetime, end: datetime) -> int:
    return int(
        (
            await session.execute(
                select(func.count(User.id)).where(User.created_at.between(start, end))
            )
        ).scalar_one()
    )


async def growth_series(session: AsyncSession, days: int = 30) -> list[dict[str, Any]]:
    since = utcnow() - timedelta(days=days)
    rows = (
        await session.execute(
            select(func.date(User.created_at), func.count(User.id))
            .where(User.created_at >= since)
            .group_by(func.date(User.created_at))
            .order_by(func.date(User.created_at))
        )
    ).all()
    return [{"date": str(r[0]), "new_users": int(r[1])} for r in rows]


async def deactivate_user_data(session: AsyncSession, user: User) -> dict[str, int]:
    """GDPR-style wipe: keep the account shell, remove personal content."""
    from app.db.models import (
        BodyMeasurement,
        ChatMessage,
        DailyLog,
        MealEntry,
        ProgressPhoto,
        WeightRecord,
    )

    counts: dict[str, int] = {}
    for model, key in (
        (ChatMessage, "messages"),
        (MealEntry, "meals"),
        (ProgressPhoto, "photos"),
        (WeightRecord, "weights"),
        (BodyMeasurement, "measurements"),
        (DailyLog, "daily_logs"),
    ):
        res = await session.execute(delete(model).where(model.user_id == user.id))
        counts[key] = int(res.rowcount or 0)

    # Clear the profile in place (not with a bulk UPDATE) so the ORM object the
    # caller is holding reflects the wipe immediately.
    user.display_name = None
    user.first_name = None
    user.last_name = None
    user.username = None
    user.age = None
    user.gender = None
    user.height_cm = None
    user.weight_kg = None
    user.target_weight_kg = None
    user.injuries = []
    user.health_conditions = []
    user.medical_notes = None
    user.allergies = []
    user.disliked_foods = []
    user.food_style_note = None
    user.goal_custom_note = None
    user.preferences = {}
    user.data_deleted_at = utcnow()
    await session.flush()

    counts["media"] = await MediaRepository(session).delete_for_user(user.id)
    return counts


def today() -> date:
    return utcnow().date()
