"""Model routing — the "which model does which job" abstraction.

Defaults come from ``.env`` (see the recommendation table in the project spec):

=========== =============================== ==================================
task        primary model                   why
=========== =============================== ==================================
consult     z-ai/glm-5.3-flash              cheapest, high-volume free answers
coach       deepseek/deepseek-v4-pro        long context + strong reasoning
vision_*    google/gemini-3.5-flash-lite    reliable non-experimental vision
stt         Whisper-Large-V3                Arabic-friendly transcription
=========== =============================== ==================================

Routing can be overridden **live** from the admin panel (``app_settings`` row
``model_routing``), so a better/cheaper vision model can be swapped in without
touching code or redeploying.  Overrides are cached in-process for
``OVERRIDE_TTL`` seconds to avoid a DB hit on every AI call.
"""
from __future__ import annotations

import logging
import time
from typing import Any

from app.ai.errors import AIBudgetExceeded
from app.config import settings
from app.constants import AITask
from app.db.repositories.ops import AIUsageRepository

logger = logging.getLogger(__name__)

ROUTING_KEY = "model_routing"
BUDGET_KEY = "global_daily_usd_cap"
OVERRIDE_TTL = 60.0  # seconds

# process-wide cache: (loaded_at, routing, budget_cap)
_cache: tuple[float, dict[str, list[str]], float | None] = (0.0, {}, None)


class ModelRouter:
    def __init__(self, ai_usage: AIUsageRepository | None = None) -> None:
        self.ai_usage = ai_usage
        self._overrides: dict[str, list[str]] = {}
        self._budget_cap: float | None = None
        self._loaded = False

    # ── live overrides ───────────────────────────────────────────────────────
    async def load_overrides(self, *, force: bool = False) -> None:
        global _cache
        now = time.monotonic()
        if not force and self._loaded and (now - _cache[0]) < OVERRIDE_TTL:
            self._overrides, self._budget_cap = _cache[1], _cache[2]
            return
        try:
            from app.db.base import session_scope
            from app.db.repositories.ops import SettingsRepository

            async with session_scope() as session:
                repo = SettingsRepository(session)
                routing = await repo.get(ROUTING_KEY)
                cap = await repo.get(BUDGET_KEY)
            overrides: dict[str, list[str]] = {}
            if isinstance(routing, dict):
                overrides = {
                    str(k): [str(m) for m in v if str(m).strip()]
                    for k, v in routing.items()
                    if isinstance(v, (list, tuple)) and v
                }
            budget = float(cap) if isinstance(cap, (int, float, str)) and str(cap) else None
            _cache = (now, overrides, budget)
            self._overrides, self._budget_cap, self._loaded = overrides, budget, True
        except Exception as exc:  # noqa: BLE001 — routing overrides are optional
            logger.debug("could not load routing overrides: %s", exc)
            self._loaded = True

    def set_override(self, task: AITask | str, models: list[str]) -> None:
        key = task.value if isinstance(task, AITask) else str(task)
        self._overrides[key] = models

    # ── resolution ───────────────────────────────────────────────────────────
    def models_for(self, task: AITask | str) -> list[str]:
        key = task.value if isinstance(task, AITask) else str(task)
        if key in self._overrides and self._overrides[key]:
            return list(self._overrides[key])
        return settings.task_models(key)

    def degrade_for(self, task: AITask | str) -> list[str]:
        """Cheapest chain used when a user hits a cap but we still answer."""
        base = self.models_for(task)
        cheap = settings.model_consult
        return base if cheap in base else [cheap, *base]

    async def ensure_budget(self) -> None:
        """Global daily USD kill-switch (spec §5.11)."""
        if self.ai_usage is None:
            return
        cap = self._budget_cap if self._budget_cap is not None else settings.global_daily_usd_cap
        if not cap or cap <= 0:
            return
        spent = await self.ai_usage.today_cost()
        if float(spent) >= cap:
            raise AIBudgetExceeded(
                f"Daily API budget reached (${float(spent):.2f} / ${cap:.2f})."
            )

    def describe(self) -> dict[str, Any]:
        return {task.value: self.models_for(task.value) for task in AITask}


def invalidate_cache() -> None:
    """Call after an admin edits routing/budget so the change applies at once."""
    global _cache
    _cache = (0.0, {}, None)


__all__ = ["ModelRouter", "ROUTING_KEY", "BUDGET_KEY", "OVERRIDE_TTL", "invalidate_cache"]
