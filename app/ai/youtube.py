"""Exercise video links (spec §3.أ: every exercise needs a demonstration video).

Resolution order — cheapest and most reliable first:

1. **DB cache** (``exercise_videos``) — no external call at all.
2. **YouTube Data API v3** when ``YOUTUBE_API_KEY`` is set.
3. **NanoGPT web search** restricted to ``youtube.com`` (no extra key needed).
4. **Deterministic search URL** — always works, zero cost, never broken links.
"""
from __future__ import annotations

import logging
import re
from datetime import timedelta
from typing import Any
from urllib.parse import quote_plus

import httpx

from app.config import settings
from app.db.models import utcnow
from app.db.repositories.ops import CacheRepository

logger = logging.getLogger(__name__)

YT_SEARCH_URL = "https://www.youtube.com/results?search_query={query}"
YT_API_URL = "https://www.googleapis.com/youtube/v3/search"

# Words that make an exercise query actually return a *demonstration* video.
_AR_SUFFIX = "طريقة أداء التمرين شرح"
_EN_SUFFIX = "exercise form tutorial"


def search_url(exercise: str, lang: str = "ar") -> str:
    """A guaranteed-working YouTube search link for an exercise."""
    suffix = _AR_SUFFIX if (lang or "ar").startswith("ar") else _EN_SUFFIX
    query = f"{exercise.strip()} {suffix}".strip()
    query = re.sub(r"\s+", " ", query)
    return YT_SEARCH_URL.format(query=quote_plus(query))


def _cache_key(exercise: str, lang: str) -> str:
    slug = re.sub(r"[^a-z0-9\u0600-\u06FF]+", "-", exercise.strip().lower()).strip("-")
    return f"{lang}:{slug[:120]}"


class VideoLinkService:
    def __init__(self, cache: CacheRepository | None = None, http: httpx.AsyncClient | None = None) -> None:
        self.cache = cache
        self._http = http

    @property
    def http(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=httpx.Timeout(15.0))
        return self._http

    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    async def link_for(self, exercise: str, lang: str = "ar") -> dict[str, Any]:
        """Return ``{url, title, source}`` for one exercise."""
        exercise = (exercise or "").strip()
        if not exercise:
            return {"url": None, "title": None, "source": "none"}

        key = _cache_key(exercise, lang)
        if self.cache is not None:
            cached = await self.cache.video(key, lang)
            if cached and cached.video_url:
                return {
                    "url": cached.video_url,
                    "title": cached.video_title,
                    "source": f"cache:{cached.source}",
                }

        result = await self._youtube_api(exercise, lang) or await self._web_search(exercise, lang)
        if result is None:
            result = {"url": search_url(exercise, lang), "title": exercise, "source": "search_url"}

        if self.cache is not None and result.get("url"):
            try:
                await self.cache.put_video(
                    key,
                    lang,
                    exercise_name=exercise[:255],
                    video_url=result["url"][:512],
                    video_title=(result.get("title") or "")[:512] or None,
                    channel=(result.get("channel") or "")[:255] or None,
                    source=result.get("source", "search_url"),
                    cache_days=settings.youtube_cache_days,
                )
            except Exception:  # noqa: BLE001 — caching must never break the answer
                logger.debug("video cache write failed", exc_info=True)
        return result

    async def links_for_many(self, exercises: list[str], lang: str = "ar") -> dict[str, str]:
        """Best-effort batch (sequential, cache-first) for a whole workout day."""
        out: dict[str, str] = {}
        for name in dict.fromkeys(exercises):
            res = await self.link_for(name, lang)
            if res.get("url"):
                out[name] = res["url"]
        return out

    # ── providers ────────────────────────────────────────────────────────────
    async def _youtube_api(self, exercise: str, lang: str) -> dict[str, Any] | None:
        if not settings.youtube_api_key:
            return None
        suffix = _AR_SUFFIX if lang.startswith("ar") else _EN_SUFFIX
        params = {
            "part": "snippet",
            "q": f"{exercise} {suffix}",
            "type": "video",
            "maxResults": settings.youtube_max_results or 1,
            "relevanceLanguage": "ar" if lang.startswith("ar") else "en",
            "safeSearch": settings.youtube_safe_search,
            "key": settings.youtube_api_key,
        }
        try:
            response = await self.http.get(YT_API_URL, params=params)
            if response.status_code != 200:
                logger.info("YouTube API %s: %s", response.status_code, response.text[:160])
                return None
            items = response.json().get("items") or []
            if not items:
                return None
            item = items[0]
            video_id = item.get("id", {}).get("videoId")
            if not video_id:
                return None
            snippet = item.get("snippet", {})
            return {
                "url": f"https://www.youtube.com/watch?v={video_id}",
                "title": snippet.get("title"),
                "channel": snippet.get("channelTitle"),
                "source": "youtube_api",
            }
        except httpx.HTTPError as exc:
            logger.info("YouTube API failed: %s", exc)
            return None

    async def _web_search(self, exercise: str, lang: str, ai_client: Any | None = None) -> dict[str, Any] | None:
        """NanoGPT ``POST /api/web`` restricted to youtube.com."""
        if not settings.has_ai_key or not settings.web_search_enabled:
            return None
        try:
            from app.ai.client import NanoGPTClient

            client = ai_client or NanoGPTClient()
            query = f"{exercise} {'شرح تمرين' if lang.startswith('ar') else 'exercise tutorial'}"
            items = await client.web_search(
                query, include_domains=["youtube.com"], max_results=5
            )
            for item in items:
                url = item.get("url") or ""
                if "youtube.com/watch" in url or "youtu.be/" in url:
                    return {
                        "url": url.split("&")[0],
                        "title": item.get("title"),
                        "source": "web_search",
                    }
        except Exception as exc:  # noqa: BLE001
            logger.info("web search for video failed: %s", exc)
        return None


async def enrich_plan_videos(plan: dict[str, Any], service: VideoLinkService, lang: str = "ar") -> dict[str, Any]:
    """Fill missing ``video_url`` fields inside a generated workout/boxing plan."""
    for week in plan.get("weeks") or []:
        for day in week.get("days") or []:
            for exercise in day.get("exercises") or []:
                if not exercise.get("video_url"):
                    res = await service.link_for(exercise.get("name", ""), lang)
                    exercise["video_url"] = res.get("url")
    for day in plan.get("days") or []:
        if day.get("video_url") is None and day.get("name"):
            res = await service.link_for(day["name"], lang)
            day["video_url"] = res.get("url")
    return plan


def cache_ttl() -> timedelta:
    return timedelta(days=settings.youtube_cache_days)


def now():  # tiny helper for callers/tests
    return utcnow()
