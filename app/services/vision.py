"""Vision features: food-photo calorie analysis and body-progress analysis.

Food flow (spec §3.ج):
1. store + optimise the image;
2. charge (first N scans/day free, then a small flat fee);
3. vision model returns structured JSON — items, grams, macros, confidence;
4. **if genuinely ambiguous** the model returns ``clarifications`` → the bot asks
   one or two precise questions instead of guessing;
5. the answer is fed back (``finalise``) and the meal is written to today's log,
   which instantly re-computes the remaining calories/macros.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

from app.ai.client import ImageInput
from app.ai.service import AIService
from app.config import settings
from app.constants import MealKind, PhotoAngle
from app.db.models import MealEntry, ProgressPhoto, User
from app.db.repositories import Repos
from app.i18n import t
from app.services.media import MediaService
from app.services.points import ChargeResult, PointsService
from app.utils.text import normalise, truncate

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class FoodAnalysis:
    meal_name: str = ""
    items: list[dict[str, Any]] = field(default_factory=list)
    calories: int = 0
    protein_g: float = 0.0
    carbs_g: float = 0.0
    fats_g: float = 0.0
    confidence: float = 0.0
    assumptions: list[str] = field(default_factory=list)
    clarifications: list[str] = field(default_factory=list)
    is_food: bool = True
    notes: str | None = None
    model: str = ""
    cost_usd: float = 0.0
    media_id: uuid.UUID | None = None
    raw: dict[str, Any] = field(default_factory=dict)
    charge: ChargeResult | None = None

    @property
    def needs_clarification(self) -> bool:
        return bool(self.clarifications)

    def as_message(self, lang: str = "ar") -> str:
        ar = lang.startswith("ar")
        item_lines = []
        for item in self.items:
            item_lines.append(
                t(
                    lang,
                    "scan.item_line",
                    name=item.get("name", "?"),
                    grams=_num(item.get("portion_g", 0)),
                    method=item.get("cooking_method") or ("غير معروف" if ar else "unknown"),
                    calories=int(item.get("calories") or 0),
                )
            )
        assumptions = ""
        if self.assumptions:
            assumptions = t(
                lang, "scan.assumptions",
                list="\n".join(f"• {a}" for a in self.assumptions[:6]),
            )
        return t(
            lang,
            "scan.result",
            meal=self.meal_name or ("وجبة" if ar else "meal"),
            items="\n".join(item_lines) or "—",
            calories=self.calories,
            protein=_num(self.protein_g),
            carbs=_num(self.carbs_g),
            fats=_num(self.fats_g),
            confidence=int(round((self.confidence or 0) * 100)),
            assumptions=assumptions,
        )


@dataclass(slots=True)
class PhotoAnalysis:
    photo: ProgressPhoto | None = None
    data: dict[str, Any] = field(default_factory=dict)
    text: str = ""
    model: str = ""
    cost_usd: float = 0.0
    charge: ChargeResult | None = None
    compared: bool = False


def _num(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    return str(int(number)) if abs(number - round(number)) < 0.05 else f"{number:.1f}"


class VisionService:
    def __init__(
        self,
        repos: Repos,
        ai: AIService,
        media: MediaService,
        *,
        points: PointsService | None = None,
    ) -> None:
        self.repos = repos
        self.ai = ai
        self.media = media
        self.points = points or PointsService(repos.economy)

    # ═══════════════════════════════════════════════════════════════════════
    #  FOOD
    # ═══════════════════════════════════════════════════════════════════════
    async def analyse_food(
        self,
        user: User,
        image: bytes,
        *,
        mime: str = "image/jpeg",
        telegram_file_id: str | None = None,
        hint: str | None = None,
        lang: str = "ar",
        charge: bool = True,
    ) -> FoodAnalysis:
        await self.repos.economy.check_limits(user, "vision")
        record, final_bytes, ai_input = await self.media.store_image(
            image,
            user_id=user.id,
            kind="food",
            telegram_file_id=telegram_file_id,
            retention_days=settings.keep_food_photos_days,
            meta={"hint": hint},
        )
        await self.repos.economy.bump_counter(user, "vision")

        result_charge: ChargeResult | None = None
        if charge:
            result_charge = await self.points.charge_food_scan(user)

        outcome = await self.ai.analyse_food(
            ImageInput(final_bytes, mime), user=user, lang=lang, user_hint=hint
        )
        data = outcome.data or {}
        analysis = _parse_food(data)
        analysis.model = outcome.model
        analysis.cost_usd = outcome.usage.cost_usd if outcome.usage else 0.0
        analysis.media_id = record.id if record else None
        analysis.raw = data
        analysis.charge = result_charge

        # not food → refund the charge immediately
        if not analysis.is_food and result_charge and result_charge.points < 0:
            await self.points.refund(user, abs(result_charge.points), "الصورة ليست طعامًا — استرداد",
                                     {"media_id": str(analysis.media_id) if analysis.media_id else None})
            analysis.charge = None
        return analysis

    async def finalise_food(
        self,
        user: User,
        analysis: FoodAnalysis,
        *,
        answers: str | None = None,
        image: tuple[bytes, str] | None = None,
        lang: str = "ar",
        hint: str | None = None,
    ) -> FoodAnalysis:
        """Second pass after the user answered the clarifying questions."""
        if not (answers and image):
            return analysis
        outcome = await self.ai.analyse_food(
            ImageInput(image[0], image[1]),
            user=user,
            lang=lang,
            user_hint=hint,
            followup_answers=answers,
        )
        data = outcome.data or {}
        refined = _parse_food(data)
        refined.clarifications = []  # we already asked once
        refined.model = outcome.model
        refined.cost_usd = (analysis.cost_usd or 0) + (outcome.usage.cost_usd if outcome.usage else 0)
        refined.media_id = analysis.media_id
        refined.raw = {**analysis.raw, "refined": data, "answers": answers}
        refined.charge = analysis.charge
        return refined

    async def log_meal(
        self,
        user: User,
        analysis: FoodAnalysis,
        *,
        slot: str | None = None,
        kind: str = MealKind.PHOTO.value,
        description: str | None = None,
        charge_points: int | None = None,
    ) -> tuple[MealEntry, dict[str, Any]]:
        """Write the meal to today's log and return the new daily balance."""
        entry = await self.repos.tracking.add_meal(
            user,
            user.local_today(),
            kind=kind,
            meal_name=analysis.meal_name or None,
            slot=slot,
            description=description,
            calories=int(analysis.calories or 0),
            protein_g=float(analysis.protein_g or 0),
            carbs_g=float(analysis.carbs_g or 0),
            fats_g=float(analysis.fats_g or 0),
            items=analysis.items,
            confidence=float(analysis.confidence or 0),
            assumptions=analysis.assumptions,
            ai_raw=analysis.raw or None,
            ai_model=analysis.model or None,
            ai_cost_usd=analysis.cost_usd or None,
            points_cost=charge_points if charge_points is not None else (
                abs(analysis.charge.points) if analysis.charge else 0
            ),
            media_id=analysis.media_id,
        )
        log = await self.repos.tracking.daily_log(user, user.local_today())
        await self.repos.tracking.recompute_day(log)
        from app.services.streak import StreakService

        await StreakService(self.repos).register_activity(user, log)
        return entry, _day_state(user, log)

    async def log_manual(
        self,
        user: User,
        *,
        name: str,
        calories: int,
        protein_g: float = 0.0,
        carbs_g: float = 0.0,
        fats_g: float = 0.0,
        slot: str | None = None,
    ) -> tuple[MealEntry, dict[str, Any]]:
        analysis = FoodAnalysis(
            meal_name=name, calories=calories, protein_g=protein_g, carbs_g=carbs_g, fats_g=fats_g,
            confidence=1.0, is_food=True,
        )
        return await self.log_meal(
            user, analysis, slot=slot, kind=MealKind.MANUAL.value, charge_points=0
        )

    async def log_skipped_meal(
        self, user: User, *, slot: str | None = None, reason: str | None = None
    ) -> dict[str, Any]:
        """Spec §3.ب — the day is recomputed immediately when a meal is missed."""
        log = await self.repos.tracking.daily_log(user, user.local_today())
        skipped = list(log.skipped_meals or [])
        from app.db.models import utcnow

        skipped.append({"slot": slot, "reason": reason, "at": utcnow().isoformat()})
        log.skipped_meals = skipped
        log.meals_planned = max(int(log.meals_planned or 0), len(skipped) + int(log.meals_logged or 0))
        await self.repos.tracking.recompute_day(log)
        await self.repos.session.flush()
        return _day_state(user, log)

    # ═══════════════════════════════════════════════════════════════════════
    #  PROGRESS PHOTOS
    # ═══════════════════════════════════════════════════════════════════════
    async def analyse_progress_photo(
        self,
        user: User,
        image: bytes,
        *,
        mime: str = "image/jpeg",
        angle: str = PhotoAngle.FRONT.value,
        caption: str | None = None,
        telegram_file_id: str | None = None,
        lang: str = "ar",
        compare: bool = True,
        charge: bool = True,
    ) -> PhotoAnalysis:
        await self.repos.economy.check_limits(user, "vision")
        record, final_bytes, ai_input = await self.media.store_image(
            image,
            user_id=user.id,
            kind="progress",
            telegram_file_id=telegram_file_id,
            retention_days=settings.media_retention_days,
            meta={"angle": angle, "caption": caption},
        )
        await self.repos.economy.bump_counter(user, "vision")
        photo = await self.repos.tracking.add_photo(
            user, media_id=record.id if record else None, angle=angle, caption=caption,
            consented=bool(user.privacy_consent_at),
        )

        result_charge = await self.points.charge_progress_scan(user) if charge else None

        images: list[ImageInput] = [ai_input]
        previous_analysis: dict[str, Any] | None = None
        compared = False
        if compare:
            older = await self.repos.tracking.photos(user.id, angle=angle, limit=2)
            older = [p for p in older if p.id != photo.id]
            if older:
                previous = older[0]
                if previous.media_id:
                    media = await self.repos.media.by_id(previous.media_id)
                    if media:
                        raw = await self.media.load(media)
                        if raw:
                            images.append(ImageInput(raw, media.mime_type or "image/jpeg"))
                            compared = True
                            previous_analysis = previous.analysis

        outcome = await self.ai.analyse_progress_photos(
            images, user=user, lang=lang, previous_analysis=previous_analysis
        )
        data = outcome.data or {}
        await self.repos.tracking.save_photo_analysis(
            photo,
            data,
            model=outcome.model,
            cost_usd=outcome.usage.cost_usd if outcome.usage else 0.0,
            points=abs(result_charge.points) if result_charge else 0,
        )
        text = render_photo_analysis(data, lang=lang, compared=compared)
        from app.services.streak import StreakService

        await StreakService(self.repos).photo_badge_if_due(user)
        return PhotoAnalysis(
            photo=photo,
            data=data,
            text=text,
            model=outcome.model,
            cost_usd=outcome.usage.cost_usd if outcome.usage else 0.0,
            charge=result_charge,
            compared=compared,
        )

    async def delete_all_photos(self, user: User) -> int:
        """Privacy action (spec §extra-14): delete every stored image of the user.

        Progress photos lose their rows *and* their files; food photos lose the
        image but the numeric meal log stays, so the user keeps their history
        without us keeping a picture of their plate.
        """
        count = 0
        photos = await self.repos.tracking.photos(user.id, limit=1000)
        for photo in photos:
            if photo.media_id:
                media = await self.repos.media.by_id(photo.media_id)
                if media and not media.purged_at and await self.media.delete(media):
                    count += 1
        await self.repos.tracking.delete_user_photos(user.id)

        for media in await self.repos.media.user_media(user.id, limit=1000):
            if media.purged_at:
                continue
            if await self.media.delete(media):
                count += 1
        return count


# ═══════════════════════════════════════════════════════════════════════════
#  helpers
# ═══════════════════════════════════════════════════════════════════════════
def _parse_food(data: dict[str, Any]) -> FoodAnalysis:
    items = [item for item in (data.get("items") or []) if isinstance(item, dict)]
    totals = data.get("totals") or {}
    calories = int(round(float(totals.get("calories") or sum(float(i.get("calories") or 0) for i in items))))
    protein = float(totals.get("protein_g") or sum(float(i.get("protein_g") or 0) for i in items))
    carbs = float(totals.get("carbs_g") or sum(float(i.get("carbs_g") or 0) for i in items))
    fats = float(totals.get("fats_g") or sum(float(i.get("fats_g") or 0) for i in items))
    return FoodAnalysis(
        meal_name=str(data.get("meal_name") or "")[:200],
        items=items,
        calories=calories,
        protein_g=round(protein, 1),
        carbs_g=round(carbs, 1),
        fats_g=round(fats, 1),
        confidence=float(data.get("confidence") or 0.0),
        assumptions=[str(a) for a in (data.get("assumptions") or [])][:8],
        clarifications=[str(q) for q in (data.get("clarifications") or [])][:2],
        is_food=bool(data.get("is_food", True)),
        notes=data.get("notes"),
        raw=data,
    )


def _day_state(user: User, log: Any) -> dict[str, Any]:
    return {
        "date": str(log.log_date),
        "target_calories": int(log.target_calories or 0),
        "consumed_calories": int(log.consumed_calories or 0),
        "remaining_calories": int(log.remaining_calories),
        "remaining": log.remaining_macros,
        "water_ml": int(log.water_ml or 0),
        "water_target_ml": int(log.water_target_ml or 0),
        "meals_logged": int(log.meals_logged or 0),
        "workout_done": bool(log.workout_done),
        "adherence": log.adherence_score,
        "balance": int(user.points_balance or 0),
    }


def logged_message(state: dict[str, Any], lang: str = "ar") -> str:
    remaining = state["remaining"]
    return t(
        lang,
        "scan.logged",
        left=state["remaining_calories"],
        p=remaining["protein_g"],
        c=remaining["carbs_g"],
        f=remaining["fats_g"],
        water=state["water_ml"],
        water_target=state["water_target_ml"],
    )


def render_photo_analysis(data: dict[str, Any], *, lang: str = "ar", compared: bool = False) -> str:
    ar = lang.startswith("ar")
    if not data:
        return t(lang, "common.error")
    lines: list[str] = []
    lines.append(
        "<b>📷 تحليل صورة التقدم</b>" + (" (مقارنة بالصورة السابقة)" if compared and ar else " (compared with the previous photo)" if compared else "")
    )
    fat = data.get("estimated_body_fat_pct_range")
    if fat:
        lines.append(f"• {'نطاق الدهون التقديري' if ar else 'Estimated body-fat range'}: {fat}")
    muscle = data.get("muscle_development")
    if muscle:
        lines.append(f"• {'التطور العضلي' if ar else 'Muscle development'}: {normalise(str(muscle))}")
    posture = data.get("posture_issues") or []
    if posture:
        lines.append(f"• {'ملاحظات الوقفة' if ar else 'Posture notes'}: " + "، ".join(map(str, posture[:4])))
    symmetry = data.get("symmetry_notes")
    if symmetry:
        lines.append(f"• {'التماثل' if ar else 'Symmetry'}: {normalise(str(symmetry))}")
    changes = data.get("visible_changes") or []
    if changes:
        lines.append("<b>• التغييرات المرئية:</b>" if ar else "<b>• Visible changes:</b>")
        lines.extend(f"   – {normalise(str(c))}" for c in changes[:5])
    weight_range = data.get("weight_estimate_kg_range")
    if weight_range:
        lines.append(f"• {'تقدير الوزن من الصورة (نطاق تقريبي!)' if ar else 'Weight range from photo (rough!)'}: {weight_range}")
    injuries = data.get("injury_or_pain_signs") or []
    if injuries:
        lines.append(f"⚠️ {'علامات محتملة' if ar else 'Possible signs'}: " + "، ".join(map(str, injuries[:3])))
    recommendations = data.get("recommendations") or []
    if recommendations:
        lines.append("<b>• توصيات:</b>" if ar else "<b>• Recommendations:</b>")
        lines.extend(f"   – {normalise(str(r))}" for r in recommendations[:5])
    quality = data.get("photo_quality_notes")
    if quality:
        lines.append(f"ℹ️ {normalise(str(quality))}")
    confidence = data.get("confidence")
    if confidence:
        lines.append(f"• {'الثقة' if ar else 'Confidence'}: {int(round(float(confidence) * 100))}%")
    return truncate("\n".join(lines), 3900)


def clarification_message(analysis: FoodAnalysis, lang: str = "ar") -> str:
    questions = "\n".join(f"{i}. {q}" for i, q in enumerate(analysis.clarifications, start=1))
    return t(lang, "scan.clarify", questions=questions)
