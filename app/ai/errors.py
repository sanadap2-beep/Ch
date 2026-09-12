"""AI-layer exceptions."""
from __future__ import annotations


class AIError(Exception):
    """Base class for every AI failure so handlers can catch one type."""

    def __init__(self, message: str, *, status: int | None = None, model: str | None = None) -> None:
        self.message = message
        self.status = status
        self.model = model
        super().__init__(message)


class AIConfigurationError(AIError):
    """Missing API key / bad model routing — a deploy-time problem."""


class AIRateLimitError(AIError):
    """429 from the provider."""


class AITimeoutError(AIError):
    """Provider did not answer in time."""


class AIAllModelsFailedError(AIError):
    """Every model in the fallback chain failed.

    ``attempts`` keeps the per-model error so the admin panel can show why.
    """

    def __init__(self, task: str, attempts: list[dict[str, str]]) -> None:
        self.task = task
        self.attempts = attempts
        detail = ", ".join(f"{a['model']}: {a['error']}" for a in attempts)
        super().__init__(f"all models failed for task '{task}' → {detail}")


class AIParseError(AIError):
    """Model answered but not in the requested JSON shape."""


class AIBudgetExceeded(AIError):
    """Global daily USD kill-switch tripped (spec §5.11)."""
