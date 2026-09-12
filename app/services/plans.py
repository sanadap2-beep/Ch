"""Programme generation & delivery: training, nutrition, boxing, mobility.

Generated JSON is stored as a :class:`WorkoutPlan` (weeks → days → exercises)
so it survives restarts, can be versioned, re-generated per phase and rendered
day-by-day.  Video links are resolved *after* generation through
:class:`VideoLinkService`, never invented by the model.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from app.ai.service import AIService
from app.ai.youtube import VideoLinkService, enrich_plan_videos
from app.constants import PlanStatus
from app.db.models import User, WorkoutPlan, WorkoutSession, utcnow
from app.db.repositories import Repos
from app.services.goal_registry import get_goal
from app.services.safety import SafetyService
from app.utils.text import normalise, truncate

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class PlanResult:
    plan: WorkoutPlan | None = None
    data: dict[str, Any] = field(default_factory=dict)
    text: str = ""
    model: str = ""
    cost_usd: float = 0.0
    warnings: list[str] = field(default_factory=list)


class PlanService:
    def __init__(
        self,
        repos: Repos,
        ai: AIService,
        *,
        videos: VideoLinkService | None = None,
        safety: SafetyService | None = None,
    ) -> None:
        self.repos = repos
        self.ai = ai
        self.videos = videos or VideoLinkService(repos.cache)
        self.safety = safety

    # ── generation ───────────────────────────────────────────────────────────
    async def generate(
        self,
        user: User,
        kind: str = "training",
        *,
        lang: str = "ar",
        weeks: int | None = None,
        extra_instructions: str | None = None,
    ) -> PlanResult:
        profile = get_goal(user.goal)
        if kind == "training" and profile.plan_kind == "boxing":
            kind = "boxing"
        if kind == "training" and profile.plan_kind == "flexibility":
            extra_instructions = (extra_instructions or "") + (
                "\nركّز على المرونة والاستطالة وإطالة العمود الفقري وتحسين الوقفة."
                if lang.startswith("ar")
                else "\nFocus on mobility, stretching, spinal decompression and posture."
            )
        weeks = weeks or profile.default_weeks

        instructions = extra_instructions or ""
        if self.safety is not None:
            notes = self.safety.plan_safety_notes(user, lang)
            if notes:
                instructions += "\n" + "\n".join(notes)

        outcome = await self.ai.generate_plan(
            user, kind=kind, lang=lang, extra_instructions=instructions.strip() or None, weeks=weeks
        )
        data = dict(outcome.data or {})
        if not data:
            return PlanResult(text=outcome.text, model=outcome.model,
                              cost_usd=outcome.usage.cost_usd if outcome.usage else 0.0,
                              warnings=["empty plan JSON"])

        # resolve real video links for every exercise
        try:
            data = await enrich_plan_videos(data, self.videos, lang=lang)
        except Exception as exc:  # noqa: BLE001 — links are a bonus, not a blocker
            logger.info("video enrichment failed: %s", exc)

        title = str(data.get("title") or default_title(kind, lang))[:255]
        plan = await self.repos.plans.create_plan(
            user,
            title=title,
            kind=kind,
            goal=user.goal,
            equipment=user.equipment,
            status=PlanStatus.ACTIVE.value,
            weeks=data.get("weeks") or [],
            schedule=build_schedule(data),
            nutrition_guide=data if kind == "nutrition" else None,
            notes=None,
            valid_from=utcnow().date(),
            valid_until=utcnow().date() + timedelta(weeks=max(1, int(data.get("weeks_total") or weeks))),
            ai_model=outcome.model,
            ai_cost_usd=outcome.usage.cost_usd if outcome.usage else 0.0,
            generation_input={"kind": kind, "weeks": weeks, "instructions": instructions[:1000]},
        )
        # keep the raw JSON for later phases / re-renders
        plan.notes = truncate(str(data.get("progression_rules") or ""), 500) or None
        await self.repos.session.flush()

        text = self.render_plan(plan, data, lang=lang)
        return PlanResult(
            plan=plan,
            data=data,
            text=text,
            model=outcome.model,
            cost_usd=outcome.usage.cost_usd if outcome.usage else 0.0,
        )

    # ── rendering ────────────────────────────────────────────────────────────
    def render_plan(self, plan: WorkoutPlan, data: dict[str, Any] | None = None, *, lang: str = "ar") -> str:
        ar = lang.startswith("ar")
        data = data or {}
        weeks = plan.weeks or []
        lines = [f"<b>📋 {plan.title}</b>"]
        lines.append(
            f"• {'الهدف' if ar else 'Goal'}: {get_goal(plan.goal).describe(lang)} | "
            f"{'المعدات' if ar else 'Equipment'}: {plan.equipment or '—'} | "
            f"{'الأسابيع' if ar else 'Weeks'}: {len(weeks)}"
        )
        safety_notes = data.get("safety_notes") or []
        restricted = data.get("restricted_exercises") or []
        if safety_notes or restricted:
            lines.append("")
            lines.append("<b>⚠️ ملاحظات السلامة</b>" if ar else "<b>⚠️ Safety notes</b>")
            lines.extend(f"• {normalise(str(s))}" for s in list(safety_notes)[:5])
            if restricted:
                lines.append(
                    ("• مستبعد لك: " if ar else "• Excluded for you: ") + "، ".join(map(str, restricted[:6]))
                )
        # week 1 overview + today's session
        if weeks:
            week = weeks[0]
            lines.append("")
            lines.append(f"<b>🗓️ {'الأسبوع الأول' if ar else 'Week 1'}: {week.get('title', '')}</b>")
            lines.append(f"• {normalise(str(week.get('focus') or ''))}"[:300])
            for day in (week.get("days") or [])[:7]:
                lines.append(self.render_day_summary(day, lang=lang))
            progression = data.get("progression_rules") or []
            if progression:
                lines.append("")
                lines.append("<b>📈 التدرج</b>" if ar else "<b>📈 Progression</b>")
                lines.extend(f"• {normalise(str(p))}" for p in list(progression)[:4])
        if data.get("meals"):
            lines.append("")
            lines.extend(self._render_meals(data, lang))
        return truncate("\n".join(lines), 3900)

    def _render_meals(self, data: dict[str, Any], lang: str) -> list[str]:
        ar = lang.startswith("ar")
        lines = [
            f"<b>🍽️ {'خطة الوجبات' if ar else 'Meal plan'}</b> — "
            f"{int(data.get('daily_calories') or 0)} {'سعرة' if ar else 'kcal'} | "
            f"بروتين {int(data.get('daily_protein_g') or 0)} جم"
        ]
        for meal in (data.get("meals") or [])[:6]:
            lines.append(
                f"• <b>{meal.get('name', '?')}</b> ({meal.get('slot', '')}): {int(meal.get('calories') or 0)} سعرة — "
                f"ب {meal.get('protein_g', 0)} / ك {meal.get('carbs_g', 0)} / د {meal.get('fats_g', 0)} | "
                f"{'التكلفة' if ar else 'cost'}: {meal.get('cost_tier', '—')}"
            )
            if meal.get("ingredients"):
                ingredients = "، ".join(
                    f"{i.get('name')} {i.get('grams')}جم" for i in (meal.get("ingredients") or [])[:6]
                )
                lines.append(f"   {ingredients}")
            if meal.get("swaps"):
                lines.append(f"   {'بدائل' if ar else 'swaps'}: {', '.join(map(str, meal['swaps'][:3]))}")
        for note in (data.get("budget_notes") or [])[:3]:
            lines.append(f"💡 {normalise(str(note))}")
        return lines

    def render_day_summary(self, day: dict[str, Any], *, lang: str = "ar") -> str:
        ar = lang.startswith("ar")
        index = day.get("day_index", "?")
        name = day.get("day_name") or ""
        focus = day.get("focus") or ""
        if day.get("is_rest"):
            return f"• <b>{'يوم' if ar else 'Day'} {index} — {name}</b>: 😴 {'راحة' if ar else 'rest'} {focus}".strip()
        exercises = day.get("exercises") or []
        return (
            f"• <b>{'يوم' if ar else 'Day'} {index} — {name}</b>: {focus} "
            f"({len(exercises)} {'تمرين' if ar else 'exercises'}, ~{day.get('estimated_minutes', 0)} {'دقيقة' if ar else 'min'})"
        )

    def render_day_full(self, day: dict[str, Any], *, lang: str = "ar") -> str:
        ar = lang.startswith("ar")
        lines = [
            f"<b>📅 {'يوم' if ar else 'Day'} {day.get('day_index', '')} — {day.get('day_name', '')}</b>",
            f"• {'التركيز' if ar else 'Focus'}: {day.get('focus', '')}",
        ]
        if day.get("is_rest"):
            note = day.get("notes") or ("استشفاء نشط: مشي 20-30 دقيقة + استطالة." if ar else "Active recovery: 20-30 min walk + stretching.")
            lines.append(f"😴 {normalise(str(note))}")
            return "\n".join(lines)
        if day.get("warmup"):
            lines.append(f"<b>{'إحماء' if ar else 'Warm-up'}:</b> " + "، ".join(map(str, day["warmup"][:4])))
        for index, exercise in enumerate(day.get("exercises") or [], start=1):
            line = (
                f"{index}. <b>{exercise.get('name', '')}</b> — {exercise.get('sets', '?')}×{exercise.get('reps', '?')} "
                f"({'راحة' if ar else 'rest'} {exercise.get('rest_seconds', 60)}{'ث' if ar else 's'})"
            )
            if exercise.get("target_muscle"):
                line += f" | {exercise['target_muscle']}"
            if exercise.get("weight_hint"):
                line += f"\n   {'الحمل' if ar else 'Load'}: {exercise['weight_hint']}"
            if exercise.get("cue"):
                line += f"\n   {normalise(str(exercise['cue']))}"
            if exercise.get("alternative_for"):
                line += f"\n   🔄 {'بديل عن' if ar else 'alternative to'}: {exercise['alternative_for']}"
            if exercise.get("video_url"):
                line += f"\n   🎥 <a href=\"{exercise['video_url']}\">{'فيديو' if ar else 'video'}</a>"
            lines.append(line)
        # boxing-style blocks
        for key, label in (
            ("skill_block", "🥊 مهارة"),
            ("conditioning_block", "⚡ تكييف"),
            ("punch_strength_block", "💥 تقوية اللكمة"),
        ):
            block = day.get(key)
            if block:
                lines.append(f"<b>{label if ar else key.replace('_', ' ').title()}</b>")
                lines.extend(f"• {normalise(str(item))}" for item in block[:6])
        if day.get("cooldown"):
            lines.append(f"<b>{'تبريد/استطالة' if ar else 'Cool-down'}:</b> " + "، ".join(map(str, day["cooldown"][:4])))
        if day.get("rounds"):
            lines.append(f"⏱️ {'جولات' if ar else 'Rounds'}: {day['rounds']}")
        if day.get("notes"):
            lines.append(f"ℹ️ {normalise(str(day['notes']))}")
        return truncate("\n".join(lines), 3900)

    # ── daily sessions ───────────────────────────────────────────────────────
    async def session_for_today(self, user: User, *, lang: str = "ar") -> tuple[WorkoutSession | None, str]:
        plan = await self.repos.plans.any_active_plan(user.id)
        if plan is None or not plan.weeks:
            return None, ""
        existing = await self.repos.plans.today_session(user.id)
        day = pick_day_for_today(plan, user)
        if existing is None:
            existing = await self.repos.plans.create_session(
                user,
                plan_id=plan.id,
                session_date=user.local_today(),
                week_index=int(day.get("week_index") or 1),
                day_index=int(day.get("day_index") or 1),
                title=f"{day.get('day_name') or ''} — {day.get('focus') or ''}".strip(" —"),
                focus=day.get("focus"),
                exercises=day.get("exercises") or [],
            )
        text = self.render_day_full(day, lang=lang)
        if existing.completed:
            text = ("✅ تم إنجاز تمرين اليوم.\n\n" if lang.startswith("ar") else "✅ Today's session is done.\n\n") + text
        return existing, text

    async def complete_today(
        self,
        user: User,
        *,
        duration_min: int | None = None,
        rpe: int | None = None,
        feedback: str | None = None,
        lang: str = "ar",
    ) -> tuple[WorkoutSession | None, dict[str, Any]]:
        session, _ = await self.session_for_today(user, lang=lang)
        if session is None:
            return None, {}
        day = pick_day_for_today(await self.repos.plans.plan_by_id(session.plan_id) if session.plan_id else None, user)
        calories = int(day.get("estimated_calories_burned") or 0) if day else 0
        await self.repos.plans.complete_session(
            session, duration_min=duration_min, rpe=rpe, feedback=feedback, calories=calories or None
        )
        log = await self.repos.tracking.daily_log(user, user.local_today())
        log.workout_done = True
        log.burned_calories_est = int(log.burned_calories_est or 0) + (calories or 0)
        log.workout_note = feedback or log.workout_note
        await self.repos.tracking.recompute_day(log)
        user.total_workouts = int(user.total_workouts or 0) + 1

        from app.services.streak import StreakService

        streak_result = await StreakService(self.repos).register_activity(user, log)
        badges = await StreakService(self.repos).check_achievement_badges(user)
        await self.repos.session.flush()
        return session, {
            "calories": calories,
            "duration": duration_min,
            "streak": streak_result.streak,
            "badges": list(dict.fromkeys([*streak_result.new_badges, *badges])),
            "adherence": log.adherence_score,
        }

    async def active_plan_summary(self, user: User, *, lang: str = "ar") -> str:
        plan = await self.repos.plans.any_active_plan(user.id)
        if plan is None:
            from app.i18n import t

            return t(lang, "plan.none")
        weeks = plan.weeks or []
        total_days = sum(len(w.get("days") or []) for w in weeks)
        header = (
            f"<b>📋 {plan.title}</b>\n"
            f"• النوع: {plan.kind} | الأسابيع: {len(weeks)} | الأيام: {total_days}\n"
            f"• الحالة: {plan.status}\n"
        )
        if weeks:
            header += "\n<b>الأسبوع الأول</b>\n"
            header += "\n".join(self.render_day_summary(d, lang=lang) for d in (weeks[0].get("days") or [])[:7])
        return truncate(header, 3900)


# ═══════════════════════════════════════════════════════════════════════════
#  helpers
# ═══════════════════════════════════════════════════════════════════════════
def build_schedule(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten weeks→days into a simple schedule used by reminders/reports."""
    schedule: list[dict[str, Any]] = []
    for week in data.get("weeks") or []:
        for day in week.get("days") or []:
            schedule.append(
                {
                    "week": week.get("week_index", 1),
                    "day": day.get("day_index"),
                    "name": day.get("day_name"),
                    "focus": day.get("focus"),
                    "is_rest": bool(day.get("is_rest")),
                    "minutes": day.get("estimated_minutes"),
                }
            )
    # boxing/flat plans keep days at the root
    for day in data.get("days") or []:
        schedule.append(
            {
                "week": data.get("phase", 1),
                "day": day.get("day_index"),
                "name": day.get("focus"),
                "focus": day.get("focus"),
                "is_rest": bool(day.get("is_rest")),
                "minutes": day.get("estimated_minutes"),
            }
        )
    return schedule


def pick_day_for_today(plan: WorkoutPlan | None, user: User) -> dict[str, Any]:
    """Map today's weekday to a plan day (week 1 by default, rotating later)."""
    if plan is None or not plan.weeks:
        return {}
    today: date = user.local_today()
    weeks = plan.weeks or []
    start = plan.valid_from or today
    week_index = min(max(0, (today - start).days // 7), len(weeks) - 1)
    week = weeks[week_index]
    days = week.get("days") or []
    if not days:
        return {}
    slot = today.weekday() % len(days)
    day = dict(days[slot])
    day.setdefault("week_index", week.get("week_index", week_index + 1))
    return day


def default_title(kind: str, lang: str = "ar") -> str:
    ar = lang.startswith("ar")
    return {
        "training": "برنامج تمارين مخصص" if ar else "Custom training programme",
        "nutrition": "خطة وجبات حسب ميزانيتك" if ar else "Budget-aware meal plan",
        "boxing": "برنامج الملاكمة — المرحلة 1" if ar else "Boxing programme — phase 1",
        "flexibility": "برنامج الاستطالة والمرونة" if ar else "Stretching & mobility programme",
    }.get(kind, "برنامج" if ar else "Programme")


async def boxing_next_phase(
    service: PlanService, user: User, *, week: int = 2, lang: str = "ar"
) -> PlanResult:
    """Spec §5.13 — a phased boxing programme instead of random tips."""
    outcome = await service.ai.generate_plan(user, kind="boxing", lang=lang, weeks=1)
    data = dict(outcome.data or {})
    if data:
        data["phase"] = week
        data = await enrich_plan_videos(data, service.videos, lang=lang)
    plan = await service.repos.plans.create_plan(
        user,
        title=f"{'الملاكمة — المرحلة' if lang.startswith('ar') else 'Boxing — phase'} {week}",
        kind="boxing",
        goal=user.goal,
        equipment=user.equipment,
        weeks=data.get("days") and [{"week_index": week, "title": data.get("title", ""), "days": data["days"]}]
        or data.get("weeks")
        or [],
        schedule=build_schedule(data),
        ai_model=outcome.model,
        ai_cost_usd=outcome.usage.cost_usd if outcome.usage else 0.0,
    )
    return PlanResult(
        plan=plan, data=data, text=service.render_plan(plan, data, lang=lang),
        model=outcome.model, cost_usd=outcome.usage.cost_usd if outcome.usage else 0.0,
    )


__all__ = [
    "PlanService",
    "PlanResult",
    "build_schedule",
    "pick_day_for_today",
    "default_title",
    "boxing_next_phase",
    "uuid",
]
