"""Repository facade — one entry point for handlers/services."""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories.economy import (
    DailyLimitReached,
    EconomyRepository,
    InsufficientPoints,
)
from app.db.repositories.ops import (
    AIUsageRepository,
    AuditRepository,
    BroadcastRepository,
    CacheRepository,
    MediaRepository,
    SettingsRepository,
)
from app.db.repositories.plans import ChatRepository, PlanRepository
from app.db.repositories.tracking import TrackingRepository
from app.db.repositories.users import UserRepository


@dataclass
class Repos:
    """Bundle of repositories bound to a single ``AsyncSession``.

    Usage::

        async with session_scope() as session:
            repos = Repos(session)
            user, created = await repos.users.get_or_create(tg_id)
    """

    session: AsyncSession

    def __post_init__(self) -> None:
        self.users = UserRepository(self.session)
        self.tracking = TrackingRepository(self.session)
        self.economy = EconomyRepository(self.session)
        self.plans = PlanRepository(self.session)
        self.chat = ChatRepository(self.session)
        self.ai_usage = AIUsageRepository(self.session)
        self.media = MediaRepository(self.session)
        self.broadcasts = BroadcastRepository(self.session)
        self.audit = AuditRepository(self.session)
        self.settings = SettingsRepository(self.session)
        self.cache = CacheRepository(self.session)


__all__ = [
    "Repos",
    "UserRepository",
    "TrackingRepository",
    "EconomyRepository",
    "PlanRepository",
    "ChatRepository",
    "AIUsageRepository",
    "MediaRepository",
    "BroadcastRepository",
    "AuditRepository",
    "SettingsRepository",
    "CacheRepository",
    "InsufficientPoints",
    "DailyLimitReached",
]
