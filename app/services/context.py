"""Per-request service container (tiny composition root).

Handlers receive one ``Services`` object from the auth middleware and reach
every feature through it.  Keeping construction in one place means a new
feature service is a two-line change and there is exactly one session per
update (so everything commits or rolls back together).
"""
from __future__ import annotations

from typing import Any

from aiogram import Bot
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai import make_service
from app.ai.service import AIService
from app.ai.youtube import VideoLinkService
from app.db.repositories import Repos
from app.services.broadcast import BroadcastService
from app.services.coach import CoachService
from app.services.consult import ConsultService
from app.services.media import MediaService
from app.services.plans import PlanService
from app.services.points import PointsService
from app.services.privacy import PrivacyService
from app.services.progress import ProgressService
from app.services.referral import ReferralService
from app.services.reminders import ReminderService
from app.services.reports import ReportService
from app.services.safety import SafetyService
from app.services.streak import StreakService
from app.services.subscription import SubscriptionService
from app.services.usage import UsageService
from app.services.vision import VisionService


class Services:
    def __init__(self, session: AsyncSession, bot: Bot | None = None) -> None:
        self.session = session
        self.bot = bot
        self.repos = Repos(session)

        # AI (shared HTTP client + per-request telemetry writer)
        self.ai: AIService = make_service(self.repos.ai_usage)

        # infrastructure
        self.media = MediaService(bot, self.repos.media)
        self.videos = VideoLinkService(self.repos.cache)

        # economy
        self.points = PointsService(self.repos.economy)
        self.subscription = SubscriptionService(self.repos, self.points)
        self.usage = UsageService(self.repos)
        self.referral = ReferralService(self.repos)

        # gamification / safety
        self.streaks = StreakService(self.repos)
        self.safety = SafetyService(self.ai, self.repos)

        # features
        self.consult = ConsultService(self.repos, self.ai, videos=self.videos, points=self.points)
        self.vision = VisionService(self.repos, self.ai, self.media, points=self.points)
        self.plans = PlanService(self.repos, self.ai, videos=self.videos, safety=self.safety)
        self.coach = CoachService(
            self.repos, self.ai, points=self.points, streaks=self.streaks, safety=self.safety
        )
        self.progress = ProgressService(self.repos, self.coach, self.streaks)
        self.reports = ReportService(self.repos, self.ai)
        self.privacy = PrivacyService(self.repos, reports=self.reports, vision=self.vision)
        self.reminders = ReminderService(self.repos, bot)

        # onboarding (built late: it depends on several of the above)
        from app.services.onboarding import OnboardingService

        self.onboarding = OnboardingService(
            self.repos,
            safety=self.safety,
            points=self.points,
            referral=self.referral,
            streaks=self.streaks,
        )

    # broadcast owns its own sessions (it outlives a single update)
    def broadcaster(self, bot: Bot | None = None, *, own_session: bool = False) -> BroadcastService:
        """Share this container's session unless the caller needs an independent one."""
        return BroadcastService(
            bot or self.bot,                                    # type: ignore[arg-type]
            None if own_session else self.repos,
        )

    async def close(self) -> None:
        await self.videos.aclose()

    def __repr__(self) -> str:  # pragma: no cover — debugging aid
        return f"<Services session={id(self.session)} bot={bool(self.bot)}>"


def build_services(session: AsyncSession, bot: Bot | None = None) -> Services:
    return Services(session, bot)


__all__ = ["Services", "build_services", "Any"]
