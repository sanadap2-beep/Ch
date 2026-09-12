"""General consultation service (spec §3.أ) — free, stateless, high volume.

Two-step by design:

1. The model answers **structurally** (prose answer + list of exercises +
   meals + one smart follow-up question).
2. Video links are resolved by :class:`VideoLinkService` — never invented by
   the model — so every exercise ships with a *working* link (spec: "لكل تمرين:
   اسم + شرح + رابط فيديو").

If the structured call fails to parse, we fall back to a plain-text answer so
the free feature never breaks.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from app.ai import prompts
from app.ai.client import Usage
from app.ai.errors import AIParseError
from app.ai.service import AIService
from app.ai.youtube import VideoLinkService
from app.config import settings
from app.constants import AITask
from app.db.models import User
from app.db.repositories import Repos
from app.db.repositories.economy import DailyLimitReached
from app.i18n import t
from app.services.points import PointsService
from app.utils.text import normalise, truncate

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ConsultResult:
    text: str
    raw: str = ""
    model: str = ""
    usage: Usage | None = None
    exercises: list[dict[str, Any]] = field(default_factory=list)
    meals: list[dict[str, Any]] = field(default_factory=list)
    follow_up: str | None = None
    safety_note: str | None = None
    cost_usd: float = 0.0


class ConsultService:
    def __init__(
        self,
        repos: Repos,
        ai: AIService,
        *,
        videos: VideoLinkService | None = None,
        points: PointsService | None = None,
    ) -> None:
        self.repos = repos
        self.ai = ai
        self.videos = videos or VideoLinkService(repos.cache)
        self.points = points or PointsService(repos.economy)

    async def ask(
        self,
        user: User,
        question: str,
        *,
        lang: str = "ar",
        history: list[dict[str, str]] | None = None,
        use_profile: bool = True,
    ) -> ConsultResult:
        await self.repos.economy.check_limits(user, "consult")
        await self.repos.economy.bump_counter(user, "consult")

        try:
            outcome = await self.ai.run(
                AITask.CONSULT,
                [
                    {"role": "system", "content": prompts.consult_system(user if use_profile else None, lang=lang)},
                    *(history or []),
                    {"role": "user", "content": truncate(question, 3000)},
                ],
                user_id=user.id,
                schema="consult",
                temperature=0.7,
                max_tokens=2200,
            )
        except AIParseError as exc:
            logger.info("structured consult failed (%s) — falling back to prose", exc)
            return await self._plain(user, question, lang, history)

        data = outcome.data or {}
        exercises = [e for e in (data.get("exercises") or []) if isinstance(e, dict)]
        meals = [m for m in (data.get("meals") or []) if isinstance(m, dict)]
        links = await self.videos.links_for_many(
            [str(e.get("video_query") or e.get("name") or "") for e in exercises], lang=lang
        )
        for exercise in exercises:
            query = str(exercise.get("video_query") or exercise.get("name") or "")
            exercise["video_url"] = links.get(query)
        text = render_consult(
            answer=str(data.get("answer") or ""),
            exercises=exercises,
            meals=meals,
            follow_up=data.get("follow_up_question"),
            safety_note=data.get("safety_note"),
            total_calories=data.get("estimated_total_calories"),
            lang=lang,
        )
        return ConsultResult(
            text=text,
            raw=outcome.text,
            model=outcome.model,
            usage=outcome.usage,
            exercises=exercises,
            meals=meals,
            follow_up=data.get("follow_up_question"),
            safety_note=data.get("safety_note"),
            cost_usd=outcome.usage.cost_usd if outcome.usage else 0.0,
        )

    async def _plain(
        self,
        user: User,
        question: str,
        lang: str,
        history: list[dict[str, str]] | None,
    ) -> ConsultResult:
        outcome = await self.ai.run(
            AITask.CONSULT,
            [
                {"role": "system", "content": prompts.consult_system(user, lang=lang)},
                *(history or []),
                {"role": "user", "content": truncate(question, 3000)},
            ],
            user_id=user.id,
            temperature=0.7,
            max_tokens=1600,
        )
        return ConsultResult(
            text=normalise(outcome.text),
            raw=outcome.text,
            model=outcome.model,
            usage=outcome.usage,
            cost_usd=outcome.usage.cost_usd if outcome.usage else 0.0,
        )

    async def remaining_today(self, user: User) -> int:
        await self.repos.economy.ensure_windows(user)
        return max(0, settings.consult_free_daily_limit - int(user.daily_consult_count or 0))


# ═══════════════════════════════════════════════════════════════════════════
#  rendering
# ═══════════════════════════════════════════════════════════════════════════
def render_consult(
    *,
    answer: str,
    exercises: list[dict[str, Any]],
    meals: list[dict[str, Any]] | None = None,
    follow_up: str | None = None,
    safety_note: str | None = None,
    total_calories: int | None = None,
    lang: str = "ar",
) -> str:
    ar = lang.startswith("ar")
    blocks: list[str] = [normalise(answer).strip()]

    if exercises:
        head = "<b>🏋️ التمارين</b>" if ar else "<b>🏋️ Exercises</b>"
        lines = [head]
        for index, exercise in enumerate(exercises, start=1):
            name = str(exercise.get("name") or "").strip()
            if not name:
                continue
            how_to = str(exercise.get("how_to") or "").strip()
            sets = str(exercise.get("sets_reps") or "").strip()
            target = str(exercise.get("target_muscle") or "").strip()
            kcal = exercise.get("calories_estimate")
            line = f"\n<b>{index}. {name}</b>"
            if sets:
                line += f" — {sets}"
            if target:
                line += f" ({target})" if ar else f" ({target})"
            if how_to:
                line += f"\n{truncate(how_to, 300)}"
            if kcal:
                line += f"\n{'🔥 ~' + str(int(kcal)) + ' سعرة' if ar else '🔥 ~' + str(int(kcal)) + ' kcal'}"
            contraindicated = exercise.get("contraindicated_for") or []
            if contraindicated:
                warn = "تجنّبه إذا: " if ar else "avoid if: "
                line += f"\n⚠️ {warn}{', '.join(map(str, contraindicated[:3]))}"
            url = exercise.get("video_url")
            if url:
                line += f"\n🎥 <a href=\"{url}\">{'فيديو شرح' if ar else 'demo video'}</a>"
            lines.append(line)
        blocks.append("\n".join(lines))

    if meals:
        head = "<b>🍽️ الوجبات</b>" if ar else "<b>🍽️ Meals</b>"
        lines = [head]
        for meal in meals:
            name = str(meal.get("name") or "").strip()
            if not name:
                continue
            line = (
                f"• <b>{name}</b>: {int(meal.get('calories') or 0)} سعرة — "
                f"بروتين {meal.get('protein_g', 0)} جم / كارب {meal.get('carbs_g', 0)} جم / دهون {meal.get('fats_g', 0)} جم"
                if ar
                else f"• <b>{name}</b>: {int(meal.get('calories') or 0)} kcal — "
                f"P {meal.get('protein_g', 0)} g / C {meal.get('carbs_g', 0)} g / F {meal.get('fats_g', 0)} g"
            )
            if meal.get("note"):
                line += f"\n  {truncate(str(meal['note']), 200)}"
            lines.append(line)
        blocks.append("\n".join(lines))

    if total_calories:
        blocks.append(
            f"🔥 <b>{'الإجمالي التقديري' if ar else 'Estimated total'}:</b> ~{int(total_calories)} "
            f"{'سعرة' if ar else 'kcal'}"
        )
    if safety_note:
        blocks.append(f"⚠️ {truncate(str(safety_note), 400)}")
    if follow_up:
        blocks.append(f"<b>❓ {truncate(str(follow_up), 300)}</b>")

    return truncate("\n\n".join(b for b in blocks if b), 3900)


def limit_message(lang: str) -> str:
    return t(lang, "consult.limit", limit=settings.consult_free_daily_limit)


def is_limit_error(exc: Exception) -> bool:
    return isinstance(exc, DailyLimitReached)
