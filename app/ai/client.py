"""NanoGPT HTTP client — the single place that talks to the AI provider.

Implements the *abstraction layer* required by the spec:

* one client, many logical tasks (``AITask``) → many models;
* automatic **fallback chain** when a model errors / times out / is overloaded;
* automatic **cost extraction** (the API echoes cost on every chat response) with
  a token-price fallback table from settings;
* retries with exponential back-off for transient failures;
* vision (image parts) and speech-to-text on the same client.

The client is intentionally *pure*: it knows nothing about Telegram or the
database.  :class:`app.ai.service.AIService` wraps it with persistence and
points accounting.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import mimetypes
import re
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.ai.errors import (
    AIAllModelsFailedError,
    AIConfigurationError,
    AIParseError,
    AIRateLimitError,
    AITimeoutError,
)
from app.config import settings
from app.constants import AITask

logger = logging.getLogger(__name__)

_RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504, 521, 522, 524}


@dataclass(slots=True)
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float = 0.0
    cost_source: str = "computed"      # api | computed | unknown

    def as_dict(self) -> dict[str, Any]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "cost_usd": round(self.cost_usd, 6),
            "cost_source": self.cost_source,
        }


@dataclass(slots=True)
class AIResult:
    text: str
    model: str
    usage: Usage = field(default_factory=Usage)
    raw: dict[str, Any] = field(default_factory=dict)
    finish_reason: str | None = None
    latency_ms: int = 0
    used_fallback: bool = False
    fallback_from: str | None = None

    @property
    def json(self) -> Any:
        """Parse the reply as JSON, tolerating code fences and stray prose."""
        return extract_json(self.text)


@dataclass(slots=True)
class ImageInput:
    """An image to send to a vision model."""

    data: bytes
    mime: str = "image/jpeg"
    detail: str = "auto"

    @classmethod
    def from_bytes(cls, data: bytes, mime: str | None = None) -> ImageInput:
        mime = mime or mimetypes.guess_type("x.jpg")[0] or "image/jpeg"
        return cls(data=data, mime=mime)

    def as_part(self) -> dict[str, Any]:
        b64 = base64.b64encode(self.data).decode("ascii")
        return {
            "type": "image_url",
            "image_url": {"url": f"data:{self.mime};base64,{b64}", "detail": self.detail},
        }


def extract_json(text: str) -> Any:
    """Best-effort JSON extraction from an LLM reply."""
    if not text:
        raise AIParseError("empty response")
    cleaned = text.strip()
    # strip ```json … ``` fences
    fence = re.match(r"^```(?:json|JSON)?\s*(.*?)\s*```$", cleaned, re.DOTALL)
    if fence:
        cleaned = fence.group(1).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    # fall back: first balanced object/array in the text
    for opener, closer in (("{", "}"), ("[", "]")):
        start = cleaned.find(opener)
        if start == -1:
            continue
        depth = 0
        in_string = False
        escaped = False
        for idx in range(start, len(cleaned)):
            ch = cleaned[idx]
            if in_string:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_string = False
                continue
            if ch == '"':
                in_string = True
            elif ch == opener:
                depth += 1
            elif ch == closer:
                depth -= 1
                if depth == 0:
                    candidate = cleaned[start : idx + 1]
                    try:
                        return json.loads(candidate)
                    except json.JSONDecodeError:
                        break
    raise AIParseError(f"could not parse JSON from model output: {cleaned[:300]}")


class NanoGPTClient:
    """Thin async wrapper around the OpenAI-compatible NanoGPT API."""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.api_key = (api_key if api_key is not None else settings.nanogpt_api_key).strip()
        self.base_url = (base_url or settings.nanogpt_base_url).rstrip("/")
        self._client = client
        self._owns_client = client is None

    # ── lifecycle ────────────────────────────────────────────────────────────
    @property
    def http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(settings.nanogpt_timeout_s, connect=15.0),
                limits=httpx.Limits(max_connections=40, max_keepalive_connections=10),
                headers={"Accept": "application/json"},
            )
            self._owns_client = True
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    def _headers(self) -> dict[str, str]:
        if not self.api_key:
            raise AIConfigurationError(
                "NANOGPT_API_KEY is not set — add it to .env (see .env.example)."
            )
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    # ── cost helpers ─────────────────────────────────────────────────────────
    def _extract_usage(self, payload: dict[str, Any], model: str) -> Usage:
        raw = payload.get("usage") or {}
        if not isinstance(raw, dict):
            raw = {}
        prompt = int(raw.get("prompt_tokens") or raw.get("input_tokens") or 0)
        completion = int(raw.get("completion_tokens") or raw.get("output_tokens") or 0)
        total = int(raw.get("total_tokens") or (prompt + completion))

        cost: float | None = None
        for holder in (raw, payload):
            for key in (
                "cost", "total_cost", "cost_usd", "usd_cost", "price",
                "nanoGPT_cost", "nano_gpt_cost", "billing_cost",
            ):
                value = holder.get(key) if isinstance(holder, dict) else None
                if isinstance(value, (int, float)):
                    cost = float(value)
                    break
                if isinstance(value, str):
                    try:
                        cost = float(value)
                        break
                    except ValueError:
                        continue
                if isinstance(value, dict):
                    for sub in ("total", "usd", "amount"):
                        if isinstance(value.get(sub), (int, float)):
                            cost = float(value[sub])
                            break
                if cost is not None:
                    break
            if cost is not None:
                break

        if cost is None:
            price_in, price_out = settings.price_of(model)
            cost = (prompt * price_in + completion * price_out) / 1_000_000
            source = "computed"
        else:
            source = "api"
        return Usage(prompt, completion, total, max(0.0, cost), source)

    # ── chat / vision ────────────────────────────────────────────────────────
    def _build_payload(
        self,
        model: str,
        messages: list[dict[str, Any]],
        *,
        images: list[ImageInput] | None,
        json_schema: dict[str, Any] | None,
        temperature: float | None,
        max_tokens: int | None,
        web_search: bool,
        extra: dict[str, Any] | None,
    ) -> dict[str, Any]:
        final_model = model
        if web_search and settings.web_search_enabled and not model.endswith(":online"):
            final_model = f"{model}:online"

        if images:
            merged: list[dict[str, Any]] = []
            injected = False
            for msg in messages:
                if not injected and msg.get("role") == "user":
                    content = msg.get("content")
                    parts: list[dict[str, Any]] = []
                    if isinstance(content, str) and content:
                        parts.append({"type": "text", "text": content})
                    elif isinstance(content, list):
                        parts.extend(content)
                    parts.extend(img.as_part() for img in images)
                    merged.append({**msg, "content": parts})
                    injected = True
                else:
                    merged.append(msg)
            if not injected:  # no user message → append one carrying the images
                merged.append({"role": "user", "content": [img.as_part() for img in images]})
            messages = merged

        payload: dict[str, Any] = {"model": final_model, "messages": messages, "stream": False}
        payload["temperature"] = (
            settings.nanogpt_temperature if temperature is None else temperature
        )
        if max_tokens or settings.nanogpt_max_tokens:
            payload["max_tokens"] = int(max_tokens or settings.nanogpt_max_tokens)
        if json_schema:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": json_schema.get("name", "schema"),
                    "strict": json_schema.get("strict", True),
                    "schema": json_schema["schema"],
                },
            }
        if settings.ai_reasoning_effort:
            payload["reasoning"] = {"effort": settings.ai_reasoning_effort}
        if extra:
            payload.update(extra)
        return payload

    async def _post_chat(self, payload: dict[str, Any]) -> dict[str, Any]:
        url = f"{self.base_url}/chat/completions"
        last_error: Exception | None = None
        for attempt in range(settings.nanogpt_max_retries + 1):
            try:
                response = await self.http.post(url, headers=self._headers(), json=payload)
            except httpx.TimeoutException as exc:
                last_error = AITimeoutError(f"timeout after {settings.nanogpt_timeout_s}s", model=payload["model"])
                logger.warning("AI timeout (attempt %s): %s", attempt + 1, exc)
            except httpx.HTTPError as exc:
                last_error = exc
                logger.warning("AI transport error (attempt %s): %s", attempt + 1, exc)
            else:
                if response.status_code == 200:
                    try:
                        return response.json()
                    except json.JSONDecodeError as exc:
                        raise AIParseError(f"invalid JSON from provider: {exc}") from exc
                if response.status_code in _RETRYABLE_STATUS:
                    last_error = AIRateLimitError(
                        f"HTTP {response.status_code}: {response.text[:200]}",
                        status=response.status_code,
                        model=payload.get("model"),
                    )
                    logger.warning("AI retryable %s: %s", response.status_code, response.text[:200])
                else:
                    raise _http_error(response, payload.get("model"))
            if attempt < settings.nanogpt_max_retries:
                await asyncio.sleep(min(2 ** attempt * 0.8, 6.0))
        if isinstance(last_error, AITimeoutError):
            raise last_error
        if isinstance(last_error, AIRateLimitError):
            raise last_error
        raise AITimeoutError(str(last_error or "unknown AI failure"), model=payload.get("model"))

    async def complete_once(
        self,
        model: str,
        messages: list[dict[str, Any]],
        *,
        images: list[ImageInput] | None = None,
        json_schema: dict[str, Any] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        web_search: bool = False,
        extra: dict[str, Any] | None = None,
    ) -> AIResult:
        payload = self._build_payload(
            model,
            messages,
            images=images,
            json_schema=json_schema,
            temperature=temperature,
            max_tokens=max_tokens,
            web_search=web_search,
            extra=extra,
        )
        started = time.perf_counter()
        data = await self._post_chat(payload)
        latency = int((time.perf_counter() - started) * 1000)

        choices = data.get("choices") or []
        if not choices:
            raise AIParseError(f"provider returned no choices: {str(data)[:300]}")
        message = choices[0].get("message") or {}
        content = message.get("content")
        if isinstance(content, list):  # some providers return content parts
            content = "".join(
                part.get("text", "") for part in content if isinstance(part, dict)
            )
        text = (content or "").strip()
        if not text:
            reasoning = message.get("reasoning") or message.get("reasoning_content")
            if reasoning:
                text = str(reasoning).strip()
        used_model = data.get("model") or payload["model"]
        return AIResult(
            text=text,
            model=str(used_model).split(":")[0],
            usage=self._extract_usage(data, used_model),
            raw=data,
            finish_reason=choices[0].get("finish_reason"),
            latency_ms=latency,
        )

    async def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        models: list[str],
        task: AITask | str = AITask.CONSULT,
        images: list[ImageInput] | None = None,
        json_schema: dict[str, Any] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        web_search: bool = False,
        extra: dict[str, Any] | None = None,
        on_attempt: Any | None = None,
    ) -> AIResult:
        """Try each model in order until one answers.

        ``on_attempt`` is an optional ``async`` callback receiving
        ``(model, result_or_none, error_or_none)`` so the caller can persist
        telemetry for *every* attempt (including failures).
        """
        task = task.value if isinstance(task, AITask) else str(task)
        if not models:
            raise AIConfigurationError(f"no models configured for task '{task}'")
        attempts: list[dict[str, str]] = []
        primary = models[0]
        for index, model in enumerate(models):
            try:
                result = await self.complete_once(
                    model,
                    messages,
                    images=images,
                    json_schema=json_schema,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    web_search=web_search,
                    extra=extra,
                )
                result.used_fallback = index > 0
                result.fallback_from = primary if index > 0 else None
                if on_attempt:
                    await on_attempt(model, result, None)
                if not result.text:
                    raise AIParseError("model returned an empty answer")
                return result
            except AIConfigurationError:
                raise
            except Exception as exc:  # noqa: BLE001 — deliberate: try next model
                logger.warning("AI task=%s model=%s failed: %s", task, model, exc)
                attempts.append({"model": model, "error": str(exc)[:400]})
                if on_attempt:
                    try:
                        await on_attempt(model, None, exc)
                    except Exception:  # noqa: BLE001 — telemetry must never break the flow
                        logger.exception("on_attempt callback failed")
        raise AIAllModelsFailedError(task, attempts)

    # ── speech to text ───────────────────────────────────────────────────────
    async def transcribe(
        self,
        audio: bytes,
        *,
        filename: str = "voice.ogg",
        models: list[str] | None = None,
        language: str | None = None,
    ) -> tuple[str, Usage, str]:
        """Return ``(text, usage, model_used)``."""
        models = models or [settings.model_stt, *settings.fallback_stt]
        url = f"{self.base_url}/audio/transcriptions"
        last_error: Exception | None = None
        for model in models:
            data = {"model": model}
            if language:
                data["language"] = language
            try:
                response = await self.http.post(
                    url,
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    data=data,
                    files={"file": (filename, audio, "application/octet-stream")},
                )
                if response.status_code != 200:
                    raise _http_error(response, model)
                payload = response.json()
                text = (payload.get("text") or "").strip()
                if not text:
                    raise AIParseError("empty transcription")
                usage = Usage(0, 0, 0, float(payload.get("cost") or 0.0), "api" if payload.get("cost") else "unknown")
                return text, usage, model
            except AIConfigurationError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning("STT model=%s failed: %s", model, exc)
                last_error = exc
        raise AIAllModelsFailedError(
            AITask.STT.value, [{"model": models[0] if models else "-", "error": str(last_error)}]
        )

    # ── direct web search (used for exercise videos) ─────────────────────────
    async def web_search(
        self,
        query: str,
        *,
        include_domains: list[str] | None = None,
        max_results: int = 5,
        provider: str | None = None,
        depth: str = "standard",
    ) -> list[dict[str, Any]]:
        """POST /api/web — returns normalised ``{url,title,snippet}`` items."""
        if not self.api_key:
            return []
        payload: dict[str, Any] = {"query": query, "depth": depth}
        if include_domains:
            payload["includeDomains"] = include_domains
        if provider:
            payload["provider"] = provider
        try:
            response = await self.http.post(
                "https://nano-gpt.com/api/web", headers=self._headers(), json=payload
            )
            if response.status_code != 200:
                logger.info("web search unavailable (%s): %s", response.status_code, response.text[:200])
                return []
            data = response.json()
        except Exception as exc:  # noqa: BLE001
            logger.info("web search failed: %s", exc)
            return []

        items = _flatten_search_results(data.get("data") or data.get("results") or [])
        return items[:max_results]


def _http_error(response: httpx.Response, model: str | None) -> Exception:
    from app.ai.errors import AIError

    body = response.text[:400]
    if response.status_code in (401, 403):
        return AIConfigurationError(
            f"NanoGPT rejected the API key ({response.status_code}): {body}",
            status=response.status_code,
            model=model,
        )
    if response.status_code == 402:
        return AIConfigurationError(
            f"NanoGPT balance/limit problem ({response.status_code}): {body}",
            status=response.status_code,
            model=model,
        )
    if response.status_code == 429:
        return AIRateLimitError(f"rate limited: {body}", status=429, model=model)
    return AIError(f"HTTP {response.status_code}: {body}", status=response.status_code, model=model)


def _flatten_search_results(raw: Any) -> list[dict[str, Any]]:
    """Search providers return wildly different shapes — normalise them."""
    out: list[dict[str, Any]] = []

    def walk(node: Any) -> None:
        if isinstance(node, list):
            for item in node:
                walk(item)
            return
        if not isinstance(node, dict):
            return
        url = node.get("url") or node.get("link") or node.get("href") or node.get("source")
        title = node.get("title") or node.get("name")
        snippet = node.get("snippet") or node.get("description") or node.get("content") or node.get("text")
        if isinstance(url, str) and url.startswith("http"):
            out.append({"url": url, "title": str(title or url), "snippet": str(snippet or "")[:500]})
            return
        for value in node.values():
            if isinstance(value, (list, dict)):
                walk(value)

    walk(raw)
    # de-duplicate, keep order
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for item in out:
        if item["url"] in seen:
            continue
        seen.add(item["url"])
        unique.append(item)
    return unique
