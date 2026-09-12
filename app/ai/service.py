"""High-level AI service: prompt assembly + model routing + telemetry.

Handlers never talk to the HTTP client directly.  They call one of the typed
methods below (``consult``, ``coach_reply``, ``analyse_food`` …) and receive a
:class:`TaskOutcome` carrying the text/JSON **and** the real token/cost numbers
needed for metered point charging (spec §7: charge by actual reply length, not
a flat fee).
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

from app.ai import prompts
from app.ai.client import AIResult, ImageInput, NanoGPTClient, Usage, extract_json
from app.ai.errors import AIParseError
from app.ai.router import ModelRouter
from app.ai.schemas import SCHEMAS
from app.constants import AITask
from app.db.repositories.ops import AIUsageRepository

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class TaskOutcome:
    task: str
    text: str
    model: str
    usage: Usage
    latency_ms: int = 0
    used_fallback: bool = False
    fallback_from: str | None = None
    data: dict[str, Any] | None = None
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.text or self.data)


class AIService:
    """Stateless wrapper: give it a client + optional usage repository."""

    def __init__(
        self,
        client: NanoGPTClient | None = None,
        *,
        usage_repo: AIUsageRepository | None = None,
        router: ModelRouter | None = None,
    ) -> None:
        self.client = client or NanoGPTClient()
        self.usage_repo = usage_repo
        self.router = router or ModelRouter(usage_repo)

    async def aclose(self) -> None:
        await self.client.aclose()

    # ── core runner ──────────────────────────────────────────────────────────
    async def run(
        self,
        task: AITask | str,
        messages: list[dict[str, Any]],
        *,
        user_id: uuid.UUID | None = None,
        images: list[ImageInput] | None = None,
        schema: str | dict[str, Any] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        web_search: bool = False,
        json_repair: bool = True,
    ) -> TaskOutcome:
        task_value = task.value if isinstance(task, AITask) else str(task)
        await self.router.load_overrides()
        await self.router.ensure_budget()

        json_schema = SCHEMAS.get(schema) if isinstance(schema, str) else schema
        models = self.router.models_for(task_value)

        attempts: list[dict[str, str]] = []

        async def on_attempt(model: str, result: AIResult | None, error: Exception | None) -> None:
            if self.usage_repo is None:
                return
            try:
                await self.usage_repo.log(
                    user_id=user_id,
                    task=task_value,
                    model=model,
                    prompt_tokens=result.usage.prompt_tokens if result else 0,
                    completion_tokens=result.usage.completion_tokens if result else 0,
                    cost_usd=result.usage.cost_usd if result else 0.0,
                    latency_ms=result.latency_ms if result else None,
                    status="ok" if result is not None else ("fallback" if error else "error"),
                    fallback_from=models[0] if model != models[0] else None,
                    error=None if result is not None else str(error)[:1000],
                )
            except Exception:  # noqa: BLE001 — telemetry never breaks a user request
                logger.exception("failed to log AI usage")

        result = await self.client.complete(
            messages,
            models=models,
            task=task_value,
            images=images,
            json_schema=json_schema,
            temperature=temperature,
            max_tokens=max_tokens,
            web_search=web_search,
            on_attempt=on_attempt,
        )

        data: dict[str, Any] | None = None
        if json_schema is not None:
            try:
                parsed = extract_json(result.text)
                data = parsed if isinstance(parsed, dict) else {"items": parsed}
            except AIParseError as exc:
                if not json_repair:
                    raise
                logger.warning("JSON parse failed for task=%s (%s) — retrying once", task_value, exc)
                repair = await self._repair_json(result.text, json_schema, task_value, user_id)
                if repair is None:
                    raise AIParseError(str(exc)) from exc
                data = repair
                result.text = str(data)

        return TaskOutcome(
            task=task_value,
            text=result.text,
            model=result.model,
            usage=result.usage,
            latency_ms=result.latency_ms,
            used_fallback=result.used_fallback,
            fallback_from=result.fallback_from,
            data=data,
            errors=[a["error"] for a in attempts],
        )

    async def _repair_json(
        self, broken: str, schema: dict[str, Any], task: str, user_id: uuid.UUID | None
    ) -> dict[str, Any] | None:
        """One cheap retry asking the model to fix its own malformed JSON."""
        messages = [
            {
                "role": "system",
                "content": "You fix malformed JSON. Return ONLY valid JSON matching the schema. No prose.",
            },
            {
                "role": "user",
                "content": (
                    f"SCHEMA:\n{schema}\n\nBROKEN OUTPUT:\n{broken[:6000]}\n\nReturn the corrected JSON only."
                ),
            },
        ]
        try:
            result = await self.client.complete(
                messages,
                models=self.router.models_for(AITask.SUMMARY),
                task=task,
                json_schema=schema,
                temperature=0.0,
                max_tokens=2000,
                on_attempt=None,
            )
            parsed = extract_json(result.text)
            return parsed if isinstance(parsed, dict) else None
        except Exception as exc:  # noqa: BLE001
            logger.info("json repair failed: %s", exc)
            return None

    # ── typed helpers ────────────────────────────────────────────────────────
    async def consult(
        self,
        question: str,
        *,
        user: Any | None = None,
        user_id: uuid.UUID | None = None,
        lang: str = "ar",
        history: list[dict[str, str]] | None = None,
        web_search: bool = False,
        max_tokens: int | None = None,
    ) -> TaskOutcome:
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": prompts.consult_system(user, lang=lang)}
        ]
        messages.extend(history or [])
        messages.append({"role": "user", "content": question})
        return await self.run(
            AITask.CONSULT,
            messages,
            user_id=user_id,
            web_search=web_search or bool(settings_web_search_default()),
            max_tokens=max_tokens or 1400,
        )

    async def coach_reply(
        self,
        message: str,
        *,
        user: Any,
        day_context: str | None = None,
        lang: str = "ar",
        history: list[dict[str, str]] | None = None,
        images: list[ImageInput] | None = None,
        max_tokens: int | None = None,
    ) -> TaskOutcome:
        task = AITask.COACH if not images else AITask.VISION_PROGRESS
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": prompts.coach_system(user, day_context=day_context, lang=lang)}
        ]
        messages.extend(history or [])
        messages.append({"role": "user", "content": message})
        return await self.run(
            task, messages, user_id=user.id, images=images, max_tokens=max_tokens or 1800
        )

    async def analyse_food(
        self,
        image: ImageInput,
        *,
        user: Any | None = None,
        lang: str = "ar",
        user_hint: str | None = None,
        followup_answers: str | None = None,
    ) -> TaskOutcome:
        system = prompts.food_followup_system(lang) if followup_answers else prompts.food_analysis_system(lang)
        extra = ""
        if user is not None:
            extra += f"\n\n{prompts.profile_block(user, lang=lang)}"
        if user_hint:
            extra += f"\n\nمعلومة من المستخدم عن الوجبة: {user_hint}"
        if followup_answers:
            extra += f"\n\nإجابات المستخدم على أسئلتك السابقة: {followup_answers}"
        messages = [
            {"role": "system", "content": system + extra},
            {"role": "user", "content": "حلّل صورة الوجبة المرفقة وأرجع JSON حسب المخطط."},
        ]
        return await self.run(
            AITask.VISION_FOOD,
            messages,
            user_id=getattr(user, "id", None),
            images=[image],
            schema="food",
            temperature=0.2,
            max_tokens=1800,
        )

    async def analyse_progress_photos(
        self,
        images: list[ImageInput],
        *,
        user: Any,
        lang: str = "ar",
        previous_analysis: dict[str, Any] | None = None,
    ) -> TaskOutcome:
        extra = ""
        if previous_analysis:
            extra = f"\n\nتحليل الصورة الأقدم (للمقارنة):\n{previous_analysis}"
        messages = [
            {"role": "system", "content": prompts.progress_photo_system(lang) + "\n\n" + prompts.profile_block(user, lang=lang) + extra},
            {
                "role": "user",
                "content": (
                    f"عدد الصور: {len(images)}. "
                    + ("قارن بين الأحدث والأقدم." if len(images) > 1 else "حلّل الصورة الحالية.")
                ),
            },
        ]
        return await self.run(
            AITask.VISION_PROGRESS,
            messages,
            user_id=user.id,
            images=images,
            schema="photo",
            temperature=0.2,
            max_tokens=1600,
        )

    async def generate_plan(
        self,
        user: Any,
        *,
        kind: str = "training",
        lang: str = "ar",
        extra_instructions: str | None = None,
        weeks: int = 4,
    ) -> TaskOutcome:
        if kind == "nutrition":
            schema, task = "nutrition", AITask.PLAN
            system = prompts.plan_generation_system(user, kind="nutrition", lang=lang)
        elif kind == "boxing":
            schema, task = "boxing", AITask.BOXING
            system = prompts.boxing_program_system(user, week=1, lang=lang)
        else:
            schema, task = "workout", AITask.PLAN
            system = prompts.plan_generation_system(user, kind="training", lang=lang)
        instruction = (
            f"ابنِ برنامجًا لـ {weeks} أسابيع." if lang.startswith("ar") else f"Build a {weeks}-week programme."
        )
        if extra_instructions:
            instruction += f"\n{extra_instructions}"
        messages = [{"role": "system", "content": system}, {"role": "user", "content": instruction}]
        return await self.run(
            task, messages, user_id=user.id, schema=schema, temperature=0.6, max_tokens=6000
        )

    async def screening(self, raw_text: str, *, lang: str = "ar", user_id: uuid.UUID | None = None) -> TaskOutcome:
        messages = [
            {"role": "system", "content": prompts.safety_screen_system(lang)},
            {"role": "user", "content": raw_text[:2000]},
        ]
        return await self.run(
            AITask.SAFETY, messages, user_id=user_id, schema="safety", temperature=0.0, max_tokens=900
        )

    async def weekly_report(
        self,
        user: Any,
        *,
        period: str = "week",
        lang: str = "ar",
        stats: dict[str, Any] | None = None,
        measurements: list[dict[str, Any]] | None = None,
        meals_summary: dict[str, Any] | None = None,
        workouts: dict[str, Any] | None = None,
        weights: list[dict[str, Any]] | None = None,
    ) -> TaskOutcome:
        payload = {
            "period": period,
            "weights": weights or [],
            "measurements": measurements or [],
            "nutrition": meals_summary or {},
            "training": workouts or {},
            "other_stats": stats or {},
        }
        body = (
            "اكتب التقرير بناءً على البيانات التالية (JSON):\n"
            f"{payload}\n\nالتزم بالتنسيق المطلوب واذكر النواقص إن وجدت."
            if lang.startswith("ar")
            else f"Write the report from this JSON data:\n{payload}\n\nFollow the required format; state gaps."
        )
        messages = [
            {"role": "system", "content": prompts.report_system(user, period=period, lang=lang)},
            {"role": "user", "content": body[:12000]},
        ]
        return await self.run(AITask.REPORT, messages, user_id=user.id, temperature=0.5, max_tokens=2200)

    async def recalc_day(
        self,
        user: Any,
        *,
        what_happened: str,
        day_context: str,
        lang: str = "ar",
        remaining_meals: int = 2,
    ) -> TaskOutcome:
        system = prompts.coach_system(user, day_context=day_context, lang=lang)
        system += (
            "\n\nمهم الآن: المستخدم أبلغ عن تغيير في يومه. أعد حساب اليوم وأرجع JSON حسب المخطط "
            f"مع اقتراح {remaining_meals} وجبات عملية من أكله المحلي تغطي المتبقي."
        )
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": what_happened},
        ]
        return await self.run(
            AITask.COACH, messages, user_id=user.id, schema="recalc", temperature=0.4, max_tokens=2000
        )

    async def meal_suggestions(
        self, user: Any, *, day_context: str, lang: str = "ar", extra: str | None = None
    ) -> TaskOutcome:
        system = prompts.meal_suggestion_system(user, lang=lang) + "\n\n" + day_context
        body = extra or (
            "اقترح وجبات اليوم ضمن المتبقي من سعراتي وماكروزي وميزانيتي."
            if lang.startswith("ar")
            else "Suggest today's meals within my remaining calories, macros and budget."
        )
        messages = [{"role": "system", "content": system}, {"role": "user", "content": body}]
        return await self.run(AITask.COACH, messages, user_id=user.id, schema="nutrition", temperature=0.6, max_tokens=3000)

    async def summarise_history(
        self, history: list[dict[str, str]], *, lang: str = "ar", user_id: uuid.UUID | None = None
    ) -> TaskOutcome:
        transcript = "\n".join(f"{m['role']}: {m['content'][:800]}" for m in history[-40:])
        messages = [
            {"role": "system", "content": prompts.summary_system(lang)},
            {"role": "user", "content": transcript[:12000]},
        ]
        return await self.run(
            AITask.SUMMARY, messages, user_id=user_id, temperature=0.2, max_tokens=900
        )

    async def transcribe(
        self, audio: bytes, *, filename: str = "voice.ogg", lang: str | None = None,
        user_id: uuid.UUID | None = None,
    ) -> tuple[str, Usage, str]:
        text, usage, model = await self.client.transcribe(
            audio, filename=filename, models=self.router.models_for(AITask.STT), language=lang
        )
        if self.usage_repo is not None:
            try:
                await self.usage_repo.log(
                    user_id=user_id,
                    task=AITask.STT.value,
                    model=model,
                    cost_usd=usage.cost_usd,
                    status="ok",
                )
            except Exception:  # noqa: BLE001
                logger.exception("failed to log STT usage")
        return text, usage, model


def settings_web_search_default() -> bool:
    """Consult answers benefit from live video links; controlled by config."""
    from app.config import settings

    return settings.web_search_enabled
