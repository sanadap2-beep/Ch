"""Privacy: explicit consent, data export and right-to-be-forgotten (spec §5.12).

The bot stores personal photos daily, so consent is explicit, versioned and
logged; every export/deletion action is recorded in the audit table.
"""
from __future__ import annotations

import json
import logging
from datetime import timedelta
from typing import Any

from app.config import settings
from app.constants import AdminAction
from app.db.models import ConsentRecord, User, utcnow
from app.db.repositories import Repos
from app.db.repositories.ops import deactivate_user_data
from app.i18n import t
from app.services.reports import ReportService
from app.services.vision import VisionService

logger = logging.getLogger(__name__)

POLICY_VERSION = "1.0"


class PrivacyService:
    def __init__(
        self,
        repos: Repos,
        *,
        reports: ReportService | None = None,
        vision: VisionService | None = None,
    ) -> None:
        self.repos = repos
        self.reports = reports
        self.vision = vision

    # ── consent ──────────────────────────────────────────────────────────────
    def policy_text(self, lang: str = "ar") -> str:
        return t(lang, "privacy.body", retention=settings.keep_food_photos_days)

    def needs_consent(self, user: User) -> bool:
        if not settings.require_privacy_consent:
            return False
        return user.privacy_consent_at is None or user.privacy_policy_version != POLICY_VERSION

    async def record_consent(self, user: User, granted: bool) -> bool:
        self.repos.session.add(
            ConsentRecord(
                user_id=user.id, kind="privacy_policy", version=POLICY_VERSION, granted=granted
            )
        )
        if granted:
            user.privacy_consent_at = utcnow()
            user.privacy_policy_version = POLICY_VERSION
        await self.repos.session.flush()
        return granted

    # ── export ───────────────────────────────────────────────────────────────
    async def export_json(self, user: User) -> dict[str, Any]:
        weights = await self.repos.tracking.weight_history(user.id)
        measurements = await self.repos.tracking.measurement_history(user.id, limit=200)
        logs = await self.repos.tracking.last_logs(user.id, days=365)
        meals = await self.repos.tracking.meals_between(
            user.id, utcnow().date() - timedelta(days=365), utcnow().date()
        )
        photos = await self.repos.tracking.photos(user.id, limit=500)
        badges = await self.repos.economy.badges_of(user.id)
        ledger = await self.repos.economy.ledger(user.id, limit=500)
        plans = await self.repos.plans.plan_history(user.id, limit=10)

        profile = {
            column: getattr(user, column)
            for column in (
                "tg_id", "username", "first_name", "display_name", "age", "gender", "height_cm",
                "weight_kg", "target_weight_kg", "activity_level", "goal", "equipment", "budget_level",
                "food_style", "food_style_note", "injuries", "health_conditions", "bmr", "tdee",
                "target_calories", "target_protein_g", "target_carbs_g", "target_fats_g",
                "water_target_ml", "points_balance", "streak_current", "streak_best",
                "language_code", "user_timezone", "created_at",
            )
            if hasattr(user, column)
        }
        return {
            "exported_at": utcnow().isoformat(),
            "policy_version": POLICY_VERSION,
            "profile": _jsonable(profile),
            "weights": [{"date": str(w.recorded_on), "kg": w.weight_kg} for w in weights],
            "measurements": [{"date": str(m.recorded_on), **m.as_dict()} for m in measurements],
            "daily_logs": [
                {
                    "date": str(log.log_date),
                    "target_calories": log.target_calories,
                    "consumed_calories": log.consumed_calories,
                    "protein_g": log.consumed_protein_g,
                    "water_ml": log.water_ml,
                    "workout_done": log.workout_done,
                    "adherence": log.adherence_score,
                }
                for log in logs
            ],
            "meals": [
                {
                    "date": str(m.entry_date),
                    "name": m.meal_name,
                    "kind": m.kind,
                    "calories": m.calories,
                    "protein_g": m.protein_g,
                    "carbs_g": m.carbs_g,
                    "fats_g": m.fats_g,
                }
                for m in meals
            ],
            "photos": [
                {"id": str(p.id), "taken_at": p.taken_at.isoformat(), "angle": p.angle, "has_analysis": bool(p.analysis)}
                for p in photos
            ],
            "badges": [b.badge_key for b in badges],
            "points_ledger": [
                {"date": e.created_at.isoformat(), "amount": e.amount, "reason": e.reason, "note": e.note}
                for e in ledger
            ],
            "plans": [
                {"title": p.title, "kind": p.kind, "status": p.status, "created_at": p.created_at.isoformat()}
                for p in plans
            ],
        }

    async def export_pdf(self, user: User, lang: str = "ar") -> bytes:
        if self.reports is None:
            raise RuntimeError("ReportService is required for PDF export")
        return await self.reports.export_pdf(user, lang=lang)

    # ── deletion ─────────────────────────────────────────────────────────────
    async def delete_photos(self, user: User, *, admin: int | None = None) -> int:
        count = 0
        if self.vision is not None:
            count = await self.vision.delete_all_photos(user)
        else:
            count = await self.repos.tracking.delete_user_photos(user.id)
        if admin is not None:
            await self.repos.audit.log(admin, AdminAction.DELETE_MEDIA, target_user_id=user.id,
                                       payload={"count": count})
        return count

    async def delete_account(self, user: User, *, admin: int | None = None) -> dict[str, int]:
        """GDPR-style wipe: personal content removed, ledger kept for accounting."""
        if self.vision is not None:
            await self.vision.delete_all_photos(user)
        counts = await deactivate_user_data(self.repos.session, user)
        user.points_balance = 0
        user.streak_current = 0
        user.is_banned = True
        user.ban_reason = "deleted by user request"
        user.privacy_consent_at = None
        if admin is not None:
            await self.repos.audit.log(admin, AdminAction.RESET_PROGRESS, target_user_id=user.id,
                                       payload=counts)
        await self.repos.session.flush()
        return counts

    async def purge_expired_media(self) -> int:
        if self.vision is None:
            return 0
        return await self.vision.media.purge_expired()


def _jsonable(value: Any) -> Any:
    return json.loads(json.dumps(value, default=str, ensure_ascii=False))


__all__ = ["PrivacyService", "POLICY_VERSION", "ConsentRecord", "_jsonable"]
