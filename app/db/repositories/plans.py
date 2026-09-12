"""Training plans, sessions and conversation memory."""
from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import date, timedelta
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.constants import COACH_HISTORY_TURNS, PlanStatus
from app.db.models import ChatMessage, User, WorkoutPlan, WorkoutSession, utcnow


class PlanRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.s = session

    # ── plans ────────────────────────────────────────────────────────────────
    async def create_plan(self, user: User, **data: Any) -> WorkoutPlan:
        # only one active plan per kind
        await self.s.execute(
            update(WorkoutPlan)
            .where(
                WorkoutPlan.user_id == user.id,
                WorkoutPlan.kind == data.get("kind", "training"),
                WorkoutPlan.status == PlanStatus.ACTIVE.value,
            )
            .values(status=PlanStatus.SUPERSEDED.value)
        )
        status = data.pop("status", PlanStatus.ACTIVE.value)
        plan = WorkoutPlan(user_id=user.id, status=status, **data)
        self.s.add(plan)
        await self.s.flush()
        return plan

    async def active_plan(self, user_id: uuid.UUID, kind: str = "training") -> WorkoutPlan | None:
        return (
            await self.s.execute(
                select(WorkoutPlan)
                .where(
                    WorkoutPlan.user_id == user_id,
                    WorkoutPlan.kind == kind,
                    WorkoutPlan.status == PlanStatus.ACTIVE.value,
                )
                .order_by(WorkoutPlan.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

    async def any_active_plan(self, user_id: uuid.UUID) -> WorkoutPlan | None:
        return (
            await self.s.execute(
                select(WorkoutPlan)
                .where(WorkoutPlan.user_id == user_id, WorkoutPlan.status == PlanStatus.ACTIVE.value)
                .order_by(WorkoutPlan.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

    async def plan_by_id(self, plan_id: uuid.UUID | str) -> WorkoutPlan | None:
        if isinstance(plan_id, str):
            plan_id = uuid.UUID(plan_id)
        return await self.s.get(WorkoutPlan, plan_id)

    async def plan_history(self, user_id: uuid.UUID, limit: int = 10) -> Sequence[WorkoutPlan]:
        return (
            await self.s.execute(
                select(WorkoutPlan)
                .where(WorkoutPlan.user_id == user_id)
                .order_by(WorkoutPlan.created_at.desc())
                .limit(limit)
            )
        ).scalars().all()

    # ── sessions ─────────────────────────────────────────────────────────────
    async def today_session(self, user_id: uuid.UUID, day: date | None = None) -> WorkoutSession | None:
        day = day or utcnow().date()
        return (
            await self.s.execute(
                select(WorkoutSession)
                .where(WorkoutSession.user_id == user_id, WorkoutSession.session_date == day)
                .order_by(WorkoutSession.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

    async def upcoming_sessions(
        self, user_id: uuid.UUID, days: int = 7
    ) -> Sequence[WorkoutSession]:
        start = utcnow().date()
        return (
            await self.s.execute(
                select(WorkoutSession)
                .where(
                    WorkoutSession.user_id == user_id,
                    WorkoutSession.session_date.between(start, start + timedelta(days=days)),
                )
                .order_by(WorkoutSession.session_date)
            )
        ).scalars().all()

    async def create_session(self, user: User, **data: Any) -> WorkoutSession:
        session = WorkoutSession(user_id=user.id, **data)
        self.s.add(session)
        await self.s.flush()
        return session

    async def complete_session(
        self,
        session: WorkoutSession,
        *,
        duration_min: int | None = None,
        rpe: int | None = None,
        feedback: str | None = None,
        calories: int | None = None,
        coach_comment: str | None = None,
    ) -> WorkoutSession:
        session.completed = True
        session.completed_at = utcnow()
        session.duration_min = duration_min or session.duration_min
        session.rpe = rpe or session.rpe
        session.feedback = feedback or session.feedback
        session.calories_burned_est = calories or session.calories_burned_est
        session.coach_comment = coach_comment or session.coach_comment
        await self.s.flush()
        return session

    async def sessions_between(
        self, user_id: uuid.UUID, start: date, end: date
    ) -> Sequence[WorkoutSession]:
        return (
            await self.s.execute(
                select(WorkoutSession)
                .where(
                    WorkoutSession.user_id == user_id,
                    WorkoutSession.session_date.between(start, end),
                )
                .order_by(WorkoutSession.session_date)
            )
        ).scalars().all()

    async def workout_stats(self, user_id: uuid.UUID, days: int = 30) -> dict[str, Any]:
        since = utcnow().date() - timedelta(days=days)
        rows = (
            await self.s.execute(
                select(WorkoutSession).where(
                    WorkoutSession.user_id == user_id, WorkoutSession.session_date >= since
                )
            )
        ).scalars().all()
        done = [r for r in rows if r.completed]
        return {
            "planned": len(rows),
            "completed": len(done),
            "completion_rate": round(100 * len(done) / len(rows), 1) if rows else 0.0,
            "total_minutes": sum(int(r.duration_min or 0) for r in done),
            "avg_rpe": round(sum(int(r.rpe or 0) for r in done) / len(done), 1) if done else 0.0,
            "calories_burned": sum(int(r.calories_burned_est or 0) for r in done),
        }


class ChatRepository:
    """Rolling conversation memory per scope (coach / consult)."""

    def __init__(self, session: AsyncSession) -> None:
        self.s = session

    async def append(
        self,
        user_id: uuid.UUID,
        scope: str,
        role: str,
        content: str,
        *,
        tokens: int | None = None,
        model: str | None = None,
        has_media: bool = False,
    ) -> ChatMessage:
        msg = ChatMessage(
            user_id=user_id,
            scope=scope,
            role=role,
            content=content,
            tokens=tokens,
            model=model,
            has_media=has_media,
        )
        self.s.add(msg)
        await self.s.flush()
        return msg

    async def history(
        self, user_id: uuid.UUID, scope: str = "coach", limit: int = COACH_HISTORY_TURNS
    ) -> list[dict[str, str]]:
        rows = (
            await self.s.execute(
                select(ChatMessage)
                .where(ChatMessage.user_id == user_id, ChatMessage.scope == scope)
                .order_by(ChatMessage.created_at.desc())
                .limit(limit)
            )
        ).scalars().all()
        rows = list(reversed(rows))
        summaries = [
            {"role": "system", "content": r.content}
            for r in rows
            if r.is_summary
        ]
        turns = [
            {"role": r.role, "content": r.content}
            for r in rows
            if not r.is_summary and r.role in {"user", "assistant", "system"}
        ]
        return summaries + turns

    async def count(self, user_id: uuid.UUID, scope: str = "coach") -> int:
        return int(
            (
                await self.s.execute(
                    select(func.count(ChatMessage.id)).where(
                        ChatMessage.user_id == user_id,
                        ChatMessage.scope == scope,
                        ChatMessage.is_summary.is_(False),
                    )
                )
            ).scalar_one()
        )

    async def store_summary(self, user_id: uuid.UUID, scope: str, summary: str) -> ChatMessage:
        msg = ChatMessage(
            user_id=user_id,
            scope=scope,
            role="system",
            content=f"[ملخص المحادثة السابقة]\n{summary}",
            is_summary=True,
        )
        self.s.add(msg)
        await self.s.flush()
        return msg

    async def clear(self, user_id: uuid.UUID, scope: str | None = None) -> int:
        from sqlalchemy import delete

        stmt = delete(ChatMessage).where(ChatMessage.user_id == user_id)
        if scope:
            stmt = stmt.where(ChatMessage.scope == scope)
        res = await self.s.execute(stmt)
        return int(res.rowcount or 0)
