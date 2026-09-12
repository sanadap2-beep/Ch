"""Referral system (spec §5.3) — free points instead of relying on payments."""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

from app.config import settings
from app.constants import BadgeKey, PointsReason
from app.db.models import ReferralEvent, User, utcnow
from app.db.repositories import Repos
from app.state import referral_base_link

logger = logging.getLogger(__name__)

START_REF_PATTERN = re.compile(r"^(?:ref[_-]?)([A-Z0-9]{4,12})$", re.IGNORECASE)


@dataclass(slots=True)
class ReferralOutcome:
    inviter: User | None = None
    event: ReferralEvent | None = None
    invitee_points: int = 0
    inviter_points: int = 0
    bonus_points: int = 0
    total_referrals: int = 0
    badge: str | None = None
    already_referred: bool = False


def parse_start_param(payload: str | None) -> str | None:
    """Extract the referral code from ``/start ref_ABC1234``."""
    if not payload:
        return None
    match = START_REF_PATTERN.match(payload.strip())
    return match.group(1).upper() if match else None


class ReferralService:
    def __init__(self, repos: Repos) -> None:
        self.repos = repos

    # ── links ────────────────────────────────────────────────────────────────
    def link_for(self, user: User) -> str:
        return f"{referral_base_link()}?start=ref_{user.referral_code}"

    def share_text(self, user: User, lang: str = "ar") -> str:
        link = self.link_for(user)
        if lang.startswith("ar"):
            return (
                "🏋️‍♂️ أنا أستخدم بوت «الكوتش الذكي» — يحسب سعراتك، يتابعك يوميًا، "
                "ويحلل صور أكلك بالذكاء الاصطناعي.\n"
                f"سجّل من رابطي واحصل على {settings.referral_invitee_points} نقطة هدية:\n{link}"
            )
        return (
            "🏋️‍♂️ I use an AI fitness & nutrition coach bot — it tracks my calories daily and "
            "analyses photos of my food.\n"
            f"Sign up with my link and get {settings.referral_invitee_points} free points:\n{link}"
        )

    # ── attribution ──────────────────────────────────────────────────────────
    async def attach_inviter(self, invitee: User, code: str | None) -> ReferralOutcome:
        """Called on ``/start`` — links the new user to the inviter (no points yet)."""
        if not settings.referral_enabled or not code:
            return ReferralOutcome()
        existing = await self.repos.economy.referral_event(invitee.id)
        if existing:
            return ReferralOutcome(already_referred=True)
        inviter = await self.repos.users.by_referral_code(code)
        if inviter is None or inviter.id == invitee.id:
            return ReferralOutcome()

        invitee.referred_by_id = inviter.id
        invitee_points = settings.referral_invitee_points
        event = await self.repos.economy.record_referral(
            inviter, invitee, inviter_points=0, invitee_points=invitee_points, qualified=False
        )
        if invitee_points:
            await self.repos.economy.change_points(
                invitee, invitee_points, PointsReason.REFERRAL_INVITEE,
                note=f"هدية إحالة من {inviter.full_name}",
                reference={"code": inviter.referral_code, "inviter": str(inviter.id)},
            )
        return ReferralOutcome(inviter=inviter, event=event, invitee_points=invitee_points)

    async def qualify(self, invitee: User) -> ReferralOutcome:
        """Called when the invitee finishes onboarding → pay the inviter."""
        if not settings.referral_enabled or not invitee.referred_by_id:
            return ReferralOutcome()
        event = await self.repos.economy.referral_event(invitee.id)
        if event and event.qualified:
            return ReferralOutcome(already_referred=True)
        inviter = await self.repos.users.by_id(invitee.referred_by_id)
        if inviter is None:
            return ReferralOutcome()

        points = settings.referral_inviter_points
        await self.repos.economy.change_points(
            inviter, points, PointsReason.REFERRAL_INVITER,
            note=f"{invitee.full_name} أكمل تسجيله عبر كودك",
            reference={"invitee": str(invitee.id), "code": inviter.referral_code},
        )
        if event is None:
            event = await self.repos.economy.record_referral(
                inviter, invitee, inviter_points=points, invitee_points=0, qualified=True
            )
        else:
            event.qualified = True
            event.inviter_points = points
        total = await self.repos.users.increment_referrals(inviter)

        bonus = 0
        badge: str | None = None
        every = settings.referral_bonus_every
        if every and total and total % every == 0 and settings.referral_bonus_points:
            bonus = settings.referral_bonus_points
            await self.repos.economy.change_points(
                inviter, bonus, PointsReason.REFERRAL_INVITER,
                note=f"مكافأة كل {every} دعوات ناجحة",
                reference={"total": total},
            )
        if total >= 5:
            if await self.repos.economy.award_badge(inviter.id, BadgeKey.REFERRED_5, {"total": total}):
                badge = BadgeKey.REFERRED_5.value
        await self.repos.session.flush()
        return ReferralOutcome(
            inviter=inviter,
            event=event,
            inviter_points=points,
            bonus_points=bonus,
            total_referrals=total,
            badge=badge,
        )

    async def stats(self, user: User) -> dict[str, Any]:
        base = await self.repos.economy.referral_stats(user.id)
        base.update(
            {
                "code": user.referral_code,
                "link": self.link_for(user),
                "next_bonus_in": (
                    settings.referral_bonus_every - (base["count"] % settings.referral_bonus_every)
                    if settings.referral_bonus_every
                    else None
                ),
            }
        )
        return base

    async def notify_inviter_payload(self, outcome: ReferralOutcome, lang: str = "ar") -> str | None:
        """Text to push to the inviter (sent from the handler with the bot)."""
        if not outcome.inviter or not outcome.inviter_points:
            return None
        name = lang.startswith("ar")
        text = (
            f"✅ صديقك انضم عبر كودك!\n+{outcome.inviter_points} نقطة"
            if name
            else f"✅ Your friend joined with your code!\n+{outcome.inviter_points} points"
        )
        if outcome.bonus_points:
            text += (
                f"\n🎁 مكافأة كل {settings.referral_bonus_every} دعوات: +{outcome.bonus_points} نقطة"
                if name
                else f"\n🎁 Every-{settings.referral_bonus_every} bonus: +{outcome.bonus_points} points"
            )
        text += (
            f"\nإجمالي دعواتك: {outcome.total_referrals}" if name else f"\nTotal referrals: {outcome.total_referrals}"
        )
        return text


def now():
    return utcnow()


__all__ = ["ReferralService", "ReferralOutcome", "parse_start_param", "ReferralEvent", "now"]
