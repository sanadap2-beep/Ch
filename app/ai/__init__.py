"""AI package facade — one shared HTTP client for the whole process."""
from __future__ import annotations

from app.ai.client import AIResult, ImageInput, NanoGPTClient, Usage, extract_json
from app.ai.errors import (
    AIAllModelsFailedError,
    AIBudgetExceeded,
    AIConfigurationError,
    AIError,
    AIParseError,
    AIRateLimitError,
    AITimeoutError,
)
from app.ai.router import ModelRouter
from app.ai.service import AIService, TaskOutcome

_client: NanoGPTClient | None = None


def get_client() -> NanoGPTClient:
    """Process-wide client so httpx connection pooling is reused."""
    global _client
    if _client is None:
        _client = NanoGPTClient()
    return _client


async def close_client() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


def make_service(usage_repo=None, router: ModelRouter | None = None) -> AIService:
    """Cheap per-request wrapper around the shared client."""
    return AIService(get_client(), usage_repo=usage_repo, router=router or ModelRouter(usage_repo))


__all__ = [
    "AIService",
    "AIResult",
    "AIError",
    "AIConfigurationError",
    "AIRateLimitError",
    "AITimeoutError",
    "AIParseError",
    "AIAllModelsFailedError",
    "AIBudgetExceeded",
    "ImageInput",
    "ModelRouter",
    "NanoGPTClient",
    "TaskOutcome",
    "Usage",
    "extract_json",
    "get_client",
    "close_client",
    "make_service",
]
