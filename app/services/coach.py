"""The 24h personal coach (spec §3.ب) — the paid, fully stateful experience.

Responsibilities
----------------
* builds the *live* day context (what's left of today, water, streak, weight trend)
  and injects it into every turn, so answers are never generic;
* understands quick logs written in free text ("وزني 81.3", "شربت كاستين ماء",
  "ما أكلت الغدا") and updates the database **before** answering;
* meters points from the real token usage of each reply;
* auto-recalibrates calories when the measured trend stalls (two flat weeks);
* keeps a rolling conversation memory and summarises it when it grows too long.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from app.ai.client import ImageInput, Usage
from app.ai.service import AIService
from app.config import settings
from app.constants import COACH_SUMMARY_TRIGGER, MealKind, PointsReason
from app.db.models import DailyLog, User
from app.db.repositories import Repos
from app.db.repositories.economy import DailyLimitReached, InsufficientPoints
from app.i18n import t
from app.services.metabolism import Recalibration, recalibrate, redistribute_remaining
from app.services.points import ChargeResult, PointsService
from app.services.safety import SafetyService
from app.services.streak import StreakService
from app.utils import validators
from app.utils.text import normalise, truncate

logger = logging.getLogger(__name__)

WEIGHT_RE = re.compile(
    r"(?:وزني|الوزن|وزن|weight|weigh(?:ed|s)?|kg|كغ|كيلو)\D{0,12}(\d{2,3}(?:[.,]\d{1,2})?)",
    re.IGNORECASE,
)
WATER_RE = re.compile(
    r"(?:ماء|مويه|ماءات|شربت|water|drink|drank|ml|مل|لتر|كاس|كوب|glass)\D{0,20}(\d{1,5}(?:[.,]\d{1,2})?)?",
    re.IGNORECASE,
)
SKIP_RE = re.compile(
    r"(?:ما\s*(?:أكلت|اكلت|تغديت|تعشيت|فطرت)|لم\s*آكل|فوت|فوّت|سكّبت|skipped|didn'?t eat|missed (?:my )?(?:meal|lunch|dinner|breakfast)|ما لحقت)",
    re.IGNORECASE,
)
WORKOUT_DONE_RE = re.compile(
    r"(?:خلصت|أنهيت|انهيت|تمرن|تمرنت|لعبت|درّبت|كملت التمرين|worked out|finished (?:the )?workout|done with training|trained today)",
    re.IGNORECASE,
)
PROTEIN_SHORT_RE = re.compile(
    r"(?:ما كملت|لم أكمل|ما قدرت أكمل|ناقص).{0,25}(?:بروتين|protein)|بروتيني.{0,12}(?:ناقص|ما كمل|لم يكتمل)|missed my protein",
    re.IGNORECASE,
)


@dataclass(slots=True)
class CoachReply:
    text: str
    model: str = ""
    usage: Usage | None = None
    charge: ChargeResult | None = None
    cost_usd: float = 0.0
    events: list[str] = field(default_factory=list)
    badges: list[str] = field(default_factory=list)
    recalibration: Recalibration | None = None
    day_state: dict[str, Any] = field(default_factory=dict)
    streak: int = 0


class CoachService:
    def __init__(
        self,
        repos: Repos,
        ai: AIService,
        *,
        points: PointsService | None = None,
        streaks: StreakService | None = None,
        safety: SafetyService | None = None,
    ) -> None:
        self.repos = repos
        self.ai = ai
        self.points = points or PointsService(repos.economy)
        self.streaks = streaks or StreakService(repos)
        self.safety = safety

    # ═══════════════════════════════════════════════════════════════════════
    #  main entry point
    # ═══════════════════════════════════════════════════════════════════════
    async def handle_message(
        self,
        user: User,
        text: str,
        *,
        lang: str = "ar",
        images: list[ImageInput] | None = None,
        charge_points: bool = True,
    ) -> CoachReply:
        await self.repos.economy.ensure_windows(user)
        if charge_points:
            # gate before spending provider money
            if settings.coach_points_minimum > int(user.points_balance or 0):
                raise InsufficientPoints(settings.coach_points_minimum, int(user.points_balance or 0))
        await self.repos.economy.check_limits(user, "coach")

        events: list[str] = []
        recal: Recalibration | None = None

        # 1) cheap local parsing first — instant data updates, no AI cost
        weight = self._extract_weight(text)
        if weight is not None:
            await self.repos.tracking.record_weight(user, weight)
            events.append(f"weight:{weight}")
            recal = await self.maybe_recalibrate(user)
            badges = await self.streaks.check_achievement_badges(user)
            events.extend(f"badge:{b}" for b in badges)

        water = self._extract_water(text)
        if water:
            await self.repos.tracking.add_water(user, user.local_today(), water)
            events.append(f"water:{water}")

        skipped = bool(SKIP_RE.search(text or ""))
        protein_short = bool(PROTEIN_SHORT_RE.search(text or ""))
        if skipped or protein_short:
            slot = _guess_slot(text, lang)
            await self.repos.tracking.log_skipped(user, slot=slot, reason=text[:200])
            events.append(f"skipped:{slot or 'meal'}")

        workout_done = bool(WORKOUT_DONE_RE.search(text or ""))
        if workout_done:
            log = await self.repos.tracking.daily_log(user, user.local_today())
            log.workout_done = True
            user.total_workouts = int(user.total_workouts or 0) + 1
            await self.repos.tracking.recompute_day(log)
            events.append("workout_done")

        streak_result = await self.streaks.register_activity(user)
        if streak_result.new_badges:
            events.extend(f"badge:{b}" for b in streak_result.new_badges)

        # 2) live context for the model
        log = await self.repos.tracking.daily_log(user, user.local_today())
        trend = await self.repos.tracking.weight_trend(user.id)
        day_context = self.build_day_context(user, log, trend=trend, lang=lang)

        # 3) conversation memory
        history = await self.repos.chat.history(user.id, scope="coach")
        if await self._needs_summary(user):
            await self._summarise(user, history, lang)
            history = await self.repos.chat.history(user.id, scope="coach")

        await self.repos.chat.append(user.id, "coach", "user", truncate(text, 4000),
                                     has_media=bool(images))

        # 4) the model
        outcome = await self.ai.coach_reply(
            text, user=user, day_context=day_context, lang=lang, history=history, images=images
        )

        # 5) metered charging from the *real* reply size
        charge: ChargeResult | None = None
        if charge_points:
            charge = await self.points.charge_ai_turn(
                user,
                outcome.usage,
                reason=PointsReason.COACH_MESSAGE,
                model=outcome.model,
                cost_usd=outcome.usage.cost_usd if outcome.usage else 0.0,
                note=truncate(text, 120),
            )
        await self.repos.economy.bump_counter(user, "coach")
        await self.repos.chat.append(
            user.id, "coach", "assistant", truncate(outcome.text, 8000),
            tokens=outcome.usage.completion_tokens if outcome.usage else None,
            model=outcome.model,
        )
        await self.repos.session.flush()

        reply_text = normalise(outcome.text)
        if skipped or protein_short:
            reply_text = _prepend_recalc_note(reply_text, log, lang)
        if recal and recal.changed:
            reply_text = f"🔧 {recal.reason_ar}\n\n{reply_text}"

        return CoachReply(
            text=truncate(reply_text, 3900),
            model=outcome.model,
            usage=outcome.usage,
            charge=charge,
            cost_usd=outcome.usage.cost_usd if outcome.usage else 0.0,
            events=events,
            badges=[e.split(":", 1)[1] for e in events if e.startswith("badge:")],
            recalibration=recal,
            day_state=self.day_state(user, log),
            streak=streak_result.streak,
        )

    # ═══════════════════════════════════════════════════════════════════════
    #  context building
    # ═══════════════════════════════════════════════════════════════════════
    def build_day_context(
        self, user: User, log: DailyLog, *, trend: dict[str, Any] | None = None, lang: str = "ar"
    ) -> str:
        from app.ai.prompts import day_context_block

        now_local = user.local_now()
        hours_left = max(0, 24 - now_local.hour - 1)
        return day_context_block(
            lang=lang,
            remaining_calories=log.remaining_calories,
            remaining_protein=log.remaining_macros["protein_g"],
            remaining_carbs=log.remaining_macros["carbs_g"],
            remaining_fats=log.remaining_macros["fats_g"],
            consumed_calories=int(log.consumed_calories or 0),
            target_calories=int(log.target_calories or 0),
            water_ml=int(log.water_ml or 0),
            water_target=int(log.water_target_ml or 0),
            meals_logged=int(log.meals_logged or 0),
            workout_done=bool(log.workout_done),
            skipped=list(log.skipped_meals or []),
            streak=int(user.streak_current or 0),
            hours_left=hours_left,
            trend=trend,
        )

    def day_state(self, user: User, log: DailyLog) -> dict[str, Any]:
        return {
            "target_calories": int(log.target_calories or 0),
            "consumed_calories": int(log.consumed_calories or 0),
            "remaining_calories": int(log.remaining_calories),
            "remaining": log.remaining_macros,
            "water_ml": int(log.water_ml or 0),
            "water_target_ml": int(log.water_target_ml or 0),
            "meals_logged": int(log.meals_logged or 0),
            "workout_done": bool(log.workout_done),
            "adherence": log.adherence_score,
            "points_balance": int(user.points_balance or 0),
        }

    async def status_block(self, user: User, *, lang: str = "ar") -> str:
        log = await self.repos.tracking.daily_log(user, user.local_today())
        trend = await self.repos.tracking.weight_trend(user.id)
        state = self.day_state(user, log)
        remaining = state["remaining"]
        ar = lang.startswith("ar")
        lines = [
            f"🎯 {int(state['target_calories'])} {'سعرة' if ar else 'kcal'} | "
            f"{'أكلت' if ar else 'eaten'} {state['consumed_calories']} | "
            f"<b>{'متبقي' if ar else 'left'} {state['remaining_calories']}</b>",
            f"🥩 {'بروتين' if ar else 'P'} {remaining['protein_g']} جم | "
            f"🍚 {'كارب' if ar else 'C'} {remaining['carbs_g']} جم | "
            f"🥑 {'دهون' if ar else 'F'} {remaining['fats_g']} جم",
            f"💧 {state['water_ml']}/{state['water_target_ml']} مل | "
            f"{'✅ تمرين اليوم تم' if state['workout_done'] else '❌ تمرين اليوم لم يتم'}",
        ]
        if trend.get("samples"):
            lines.append(
                f"⚖️ {'اتجاه' if ar else 'trend'}: {trend['per_week_kg']:+.2f} {'كغ/أسبوع' if ar else 'kg/wk'}"
                + (" — ⚠️ ثبات" if trend.get("stalled") and ar else " — ⚠️ stalled" if trend.get("stalled") else "")
            )
        lines.append(self.streaks.summary(user, lang))
        if self.safety is not None:
            line = self.safety.warning_line(user, lang)
            if line:
                lines.append(f"⚠️ {line}")
        return "\n".join(lines)

    # ═══════════════════════════════════════════════════════════════════════
    #  automatic re-calibration
    # ═══════════════════════════════════════════════════════════════════════
    async def maybe_recalibrate(self, user: User) -> Recalibration | None:
        """Spec: weight flat for two weeks → change the calories automatically."""
        trend = await self.repos.tracking.weight_trend(user.id, weeks=settings_stall_weeks())
        if int(trend.get("samples") or 0) < 2:
            return None
        today = user.local_today()
        if user.last_stall_check_date and (today - user.last_stall_check_date).days < 7:
            return None
        result = recalibrate(user, trend)
        user.last_stall_check_date = today
        if result.changed:
            user.target_calories = result.new_calories
            from app.services.goal_registry import get_goal
            from app.services.metabolism import macro_split

            profile = get_goal(user.goal)
            protein, carbs, fats = macro_split(
                result.new_calories,
                float(user.weight_kg or 0),
                protein_g_per_kg=profile.protein_g_per_kg,
                fat_g_per_kg=profile.fat_g_per_kg,
                carb_style=profile.carb_style,
                height_cm=user.height_cm,
            )
            user.target_protein_g, user.target_carbs_g, user.target_fats_g = protein, carbs, fats
            adjustments = list(user.plan_adjustments or [])
            adjustments.append(
                {
                    "date": today.isoformat(),
                    "reason": "auto-recalibration",
                    "detail": result.reason,
                    "delta": result.delta,
                    "calories": result.new_calories,
                    "trend": trend,
                }
            )
            user.plan_adjustments = adjustments[-30:]
            from app.db.models import utcnow

            user.plan_updated_at = utcnow()
        await self.repos.session.flush()
        return result

    # ═══════════════════════════════════════════════════════════════════════
    #  skipped meals / redistribution
    # ═══════════════════════════════════════════════════════════════════════
    async def redistribute(self, user: User, *, meals_left: int, lang: str = "ar") -> list[dict[str, Any]]:
        log = await self.repos.tracking.daily_log(user, user.local_today())
        return redistribute_remaining(
            remaining_calories=log.remaining_calories,
            remaining_protein_g=log.remaining_macros["protein_g"],
            remaining_carbs_g=log.remaining_macros["carbs_g"],
            remaining_fats_g=log.remaining_macros["fats_g"],
            meals_left=meals_left,
        )

    # ═══════════════════════════════════════════════════════════════════════
    #  memory management
    # ═══════════════════════════════════════════════════════════════════════
    async def _needs_summary(self, user: User) -> bool:
        count = await self.repos.chat.count(user.id, "coach")
        return count >= COACH_SUMMARY_TRIGGER

    async def _summarise(self, user: User, history: list[dict[str, str]], lang: str) -> None:
        if not history:
            return
        try:
            outcome = await self.ai.summarise_history(history, lang=lang, user_id=user.id)
            await self.repos.chat.store_summary(user.id, "coach", truncate(outcome.text, 3000))
            # drop the oldest raw turns we just compressed
            keep = settings_coach_history()
            if len(history) > keep:
                await self.repos.chat.clear(user.id, "coach")
                for message in history[-keep:]:
                    await self.repos.chat.append(user.id, "coach", message["role"], message["content"])
                await self.repos.chat.store_summary(user.id, "coach", truncate(outcome.text, 3000))
            logger.info("coach history summarised for user=%s", user.tg_id)
        except Exception as exc:  # noqa: BLE001 — summarisation is best-effort
            logger.info("summary failed: %s", exc)

    # ═══════════════════════════════════════════════════════════════════════
    #  quick parsers
    # ═══════════════════════════════════════════════════════════════════════
    @staticmethod
    def _extract_weight(text: str | None) -> float | None:
        if not text:
            return None
        cleaned = validators.normalize_digits(text)
        match = WEIGHT_RE.search(cleaned)
        if not match:
            return None
        value = validators.parse_float(match.group(1), min_value=25, max_value=400)
        return value

    @staticmethod
    def _extract_water(text: str | None) -> int | None:
        if not text:
            return None
        cleaned = validators.normalize_digits(text)
        if not any(word in cleaned.lower() for word in ("ماء", "مويه", "شربت", "water", "drink", "ml", "مل", "لتر", "كاس", "كوب", "glass")):
            return None
        return validators.parse_water(cleaned)


def settings_stall_weeks() -> int:
    from app.constants import STALL_WEEKS_BEFORE_ADJUST

    return STALL_WEEKS_BEFORE_ADJUST


def settings_coach_history() -> int:
    from app.constants import COACH_HISTORY_TURNS

    return COACH_HISTORY_TURNS


def _guess_slot(text: str, lang: str) -> str | None:
    lowered = (text or "").lower()
    if any(w in lowered for w in ("فطور", "إفطار", "افطار", "breakfast", "الصباح")):
        return "breakfast"
    if any(w in lowered for w in ("غدا", "غداء", "lunch", "الظهر")):
        return "lunch"
    if any(w in lowered for w in ("عشا", "عشاء", "dinner", "المساء")):
        return "dinner"
    if any(w in lowered for w in ("سناك", "snack", "وجبة خفيفة")):
        return "snack"
    return None


def _prepend_recalc_note(text: str, log: DailyLog, lang: str) -> str:
    ar = lang.startswith("ar")
    note = (
        f"🔁 <b>أعدت حساب يومك فورًا:</b> متبقي {log.remaining_calories} سعرة — "
        f"بروتين {log.remaining_macros['protein_g']} جم، كارب {log.remaining_macros['carbs_g']} جم، "
        f"دهون {log.remaining_macros['fats_g']} جم.\n\n"
        if ar
        else f"🔁 <b>Day recalculated instantly:</b> {log.remaining_calories} kcal left — "
        f"P {log.remaining_macros['protein_g']} g, C {log.remaining_macros['carbs_g']} g, "
        f"F {log.remaining_macros['fats_g']} g.\n\n"
    )
    return note + text


def insufficient_message(need: int, have: int, lang: str = "ar") -> str:
    return t(lang, "coach.insufficient", need=need, have=have)


def limit_message(limit: int, lang: str = "ar") -> str:
    return t(lang, "coach.daily_limit", limit=limit)


def is_limit_error(exc: Exception) -> bool:
    return isinstance(exc, DailyLimitReached)


def meal_kind_from_text(text: str) -> str:
    return MealKind.TEXT.value if text else MealKind.MANUAL.value


__all__ = [
    "CoachService",
    "CoachReply",
    "insufficient_message",
    "limit_message",
    "is_limit_error",
    "meal_kind_from_text",
    "timedelta",
]
