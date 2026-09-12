"""Application settings — single source of truth for every tunable.

All values come from environment variables / a ``.env`` file (see
``.env.example``).  Nothing is hard-coded in the business logic: model routing,
point prices, daily caps, storage backend and forced-subscription channels are
all configurable at deploy time, and the most volatile ones (model routing,
pricing, caps) can additionally be overridden live from the admin panel via the
``app_settings`` table (see :mod:`app.services.runtime_config`).
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from app.constants import AITask

BASE_DIR = Path(__file__).resolve().parent.parent


def _split_csv(value: Any) -> Any:
    """Allow ``"1,2,3"``, ``'[1,2,3]'`` and a bare ``1`` in env vars for list fields.

    pydantic-settings JSON-decodes the env value of a complex field *before* the
    ``mode="before"`` validator runs, so a single value arrives already parsed:
    ``ADMIN_IDS=123456789`` — the common one-admin case — reaches the validator as
    an ``int``, not a string. Returning it unchanged made the list comprehension
    downstream raise ``TypeError: 'int' object is not iterable``, which killed the
    boot with an error that never mentions ADMIN_IDS. Wrap scalars instead.
    """
    if value is None or isinstance(value, (list, tuple)):
        return value
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return []
        if raw.startswith("["):
            return json.loads(raw)
        return [item.strip() for item in raw.split(",") if item.strip()]
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return [value]
    return [value]


def _parse_json_map(value: Any) -> Any:
    if value in (None, "") or isinstance(value, dict):
        return value or {}
    if isinstance(value, str):
        return json.loads(value)
    return value


class Settings(BaseSettings):
    # Every list/dict field is wrapped in ``NoDecode``: pydantic-settings would
    # otherwise JSON-decode its env value *before* the mode="before" validators
    # run, so the documented comma-separated form (ADMIN_IDS=111,222) raised
    # SettingsError and a single value (ADMIN_IDS=123) arrived as an int and blew
    # up the validator with "TypeError: 'int' object is not iterable". With
    # NoDecode the raw string reaches _split_csv, which accepts all three forms.
    model_config = SettingsConfigDict(
        env_file=(BASE_DIR / ".env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── runtime ──────────────────────────────────────────────────────────────
    app_env: str = "production"                       # development | production
    log_level: str = "INFO"
    timezone: str = "UTC"                             # used for reminders/reports
    sentry_dsn: str | None = None

    # ── Telegram bot ─────────────────────────────────────────────────────────
    bot_token: str = ""
    admin_ids: Annotated[list[int], NoDecode] = Field(default_factory=list)
    support_username: str | None = None               # @handle shown on errors
    bot_description: str = "كوتش رياضي وتغذية بالذكاء الاصطناعي"

    # Polling is the default; set webhook_url to run behind HTTPS.
    telegram_api_base: str | None = None              # self-hosted Bot API server (optional)
    protect_content: bool = False                     # forbid forwarding of bot media
    webhook_url: str | None = None                    # https://bot.example.com
    webhook_path: str = "/webhook"
    webhook_secret: str | None = None
    webhook_port: int = 8080
    webhook_host: str = "0.0.0.0"

    # FSM / cache storage
    fsm_storage: str = "memory"                       # memory | redis
    redis_url: str = "redis://localhost:6379/0"

    # ── database ─────────────────────────────────────────────────────────────
    database_url: str = "postgresql+asyncpg://coach:coach@localhost:5432/coach"
    db_echo: bool = False
    db_auto_create: bool = True                       # create_all on boot (dev)
    db_pool_size: int = 10
    db_max_overflow: int = 20
    db_pool_recycle: int = 1800

    # ── media storage ────────────────────────────────────────────────────────
    storage_backend: str = "local"                    # local | s3
    local_media_dir: Path = BASE_DIR / "storage" / "media"
    public_media_base_url: str | None = None          # to build direct links
    s3_bucket: str = ""
    s3_region: str = ""
    s3_endpoint_url: str | None = None                # for S3-compatible (R2/MinIO)
    s3_access_key_id: str = ""
    s3_secret_access_key: str = ""
    s3_prefix: str = "coach-bot"
    s3_acl: str | None = None
    max_photo_mb: int = 12
    photo_max_side_px: int = 1280                     # downscale before storing/AI
    photo_jpeg_quality: int = 82
    media_retention_days: int = 180                   # privacy: auto-purge age
    keep_food_photos_days: int = 90

    # ── NanoGPT (AI abstraction layer) ───────────────────────────────────────
    nanogpt_api_key: str = ""
    nanogpt_base_url: str = "https://nano-gpt.com/api/v1"
    nanogpt_timeout_s: float = 120.0
    nanogpt_max_retries: int = 2
    nanogpt_temperature: float = 0.75
    nanogpt_max_tokens: int = 1600
    # Task → ordered model chain (first = primary, rest = fallbacks).
    model_routing: Annotated[dict[str, list[str]], NoDecode] = Field(default_factory=dict)
    model_consult: str = "z-ai/glm-5.3-flash"
    model_coach: str = "deepseek/deepseek-v4-pro"
    model_vision: str = "google/gemini-3.5-flash-lite"
    model_plan: str = "deepseek/deepseek-v4-pro"
    model_summary: str = "z-ai/glm-5.3-flash"
    model_stt: str = "Whisper-Large-V3"
    fallback_consult: Annotated[list[str], NoDecode] = Field(default_factory=lambda: ["deepseek/deepseek-v4-flash"])
    fallback_coach: Annotated[list[str], NoDecode] = Field(default_factory=lambda: ["z-ai/glm-5.3-flash"])
    fallback_vision: Annotated[list[str], NoDecode] = Field(default_factory=lambda: ["google/gemini-3.5-flash"])
    fallback_stt: Annotated[list[str], NoDecode] = Field(default_factory=lambda: ["Wizper", "gpt-4o-mini-transcribe"])
    web_search_enabled: bool = True                   # use ':online' suffix / web_search
    ai_reasoning_effort: str | None = None            # none|low|medium|high (if supported)

    # voice notes → text (spec extra §8)
    voice_enabled: bool = True
    voice_max_seconds: int = 180
    voice_charge_points: int = 0                      # 0 = free (STT cost is metered by usage caps)

    # Cost table (USD per 1M tokens) used only when the API does not echo cost.
    model_pricing: Annotated[dict[str, list[float]], NoDecode] = Field(
        default_factory=lambda: {
            "z-ai/glm-5.3-flash": [0.08, 0.25],
            "deepseek/deepseek-v4-pro": [1.10, 2.20],
            "deepseek/deepseek-v4-flash": [0.25, 0.60],
            "google/gemini-3.5-flash-lite": [0.30, 2.50],
            "google/gemini-3.5-flash": [0.60, 4.00],
        }
    )
    default_pricing: Annotated[list[float], NoDecode] = Field(default_factory=lambda: [1.0, 3.0])

    # ── points economy ───────────────────────────────────────────────────────
    points_per_usd: int = 100                # 1 USD == 100 points
    welcome_bonus_points: int = 20
    coach_access_cost_points: int = 100      # spec: زر الكوتش الشخصي = 100 نقطة
    coach_access_days: int = 30              # 0 = permanent unlock
    food_scan_cost_points: int = 5           # spec: "مجاني أو بنقاط أقل"
    food_scan_free_daily: int = 3
    progress_scan_cost_points: int = 8
    report_export_cost_points: int = 0

    # Token-based metering for the personal coach (not a flat fee).
    coach_points_per_1k_input: float = 0.05
    coach_points_per_1k_output: float = 0.35
    coach_points_minimum: int = 2
    coach_points_maximum: int = 40

    # ── usage caps (spec §5.11 — protect the API balance) ────────────────────
    consult_free_daily_limit: int = 12       # free general consultations / day
    coach_daily_message_limit: int = 40
    vision_daily_limit: int = 30
    user_daily_points_cap: int = 800
    user_monthly_points_cap: int = 12000
    global_daily_usd_cap: float = 25.0       # kill-switch for the whole bot
    cap_action: str = "degrade"              # degrade (cheaper model) | block

    # ── referral / gamification ──────────────────────────────────────────────
    referral_enabled: bool = True
    referral_inviter_points: int = 25
    referral_invitee_points: int = 15
    referral_bonus_every: int = 5            # extra bonus every N successful refs
    referral_bonus_points: int = 100
    streak_reward_points: int = 10           # per completed streak milestone day
    streak_milestones: Annotated[list[int], NoDecode] = Field(default_factory=lambda: [3, 7, 14, 30, 60, 100])

    # ── subscriptions (spec §5.9) ────────────────────────────────────────────
    monthly_plan_points: int = 1500
    quarterly_plan_points: int = 4000

    # ── forced channel subscription (spec §4) ────────────────────────────────
    forced_channels_enabled: bool = False
    forced_channels: Annotated[list[str], NoDecode] = Field(default_factory=list)   # @username or -100id

    # ── YouTube links for exercises ──────────────────────────────────────────
    youtube_api_key: str | None = None
    youtube_language: str = "ar"
    youtube_max_results: int = 1
    youtube_safe_search: str = "moderate"
    youtube_cache_days: int = 30

    # ── reminders / scheduler ────────────────────────────────────────────────
    reminders_enabled: bool = True
    water_reminder_interval_min: int = 120
    default_weigh_in_day: int = 5            # 0=Mon … 6=Sun
    default_weigh_in_hour: int = 8
    default_workout_hour: int = 18
    quiet_hours_start: int = 23              # no notifications 23:00 →
    quiet_hours_end: int = 7                 # …07:00 local user time
    report_weekday: int = 6                  # weekly report day
    report_hour: int = 10
    broadcast_batch_size: int = 25
    broadcast_delay_ms: int = 60             # Telegram ~30 msg/s global limit

    # ── nutrition safety guard-rails ─────────────────────────────────────────
    min_calories_male: int = 1500
    min_calories_female: int = 1200
    max_calories: int = 4500
    max_deficit_pct: float = 0.25
    max_surplus_pct: float = 0.20
    protein_g_per_kg_cut: float = 2.2
    protein_g_per_kg_bulk: float = 1.8
    protein_g_per_kg_default: float = 1.8
    min_protein_g: int = 60

    # ── privacy (spec §5.12) ─────────────────────────────────────────────────
    require_privacy_consent: bool = True
    privacy_policy_url: str | None = None
    allow_data_export: bool = True
    data_export_cost_points: int = 0

    # ── fonts (PDF / charts Arabic rendering) ────────────────────────────────
    fonts_dir: Path = BASE_DIR / "assets" / "fonts"
    arabic_font_regular: str | None = None
    arabic_font_bold: str | None = None

    # ── validators ───────────────────────────────────────────────────────────
    @field_validator("admin_ids", mode="before")
    @classmethod
    def _v_admin_ids(cls, v: Any) -> Any:
        return [int(x) for x in (_split_csv(v) or [])]

    @field_validator(
        "fallback_consult",
        "fallback_coach",
        "fallback_vision",
        "fallback_stt",
        "forced_channels",
        mode="before",
    )
    @classmethod
    def _v_csv_str(cls, v: Any) -> Any:
        return _split_csv(v)

    @field_validator("streak_milestones", mode="before")
    @classmethod
    def _v_csv_int(cls, v: Any) -> Any:
        return [int(x) for x in (_split_csv(v) or [])]

    @field_validator("default_pricing", mode="before")
    @classmethod
    def _v_csv_float(cls, v: Any) -> Any:
        """``DEFAULT_PRICING=1.5,4.5`` as well as ``[1.5, 4.5]``.

        Every NoDecode field needs a validator that turns the raw env string into
        a list; without one, pydantic rejects the string outright.
        """
        return [float(x) for x in (_split_csv(v) or [])]

    @field_validator("model_routing", "model_pricing", mode="before")
    @classmethod
    def _v_json_map(cls, v: Any) -> Any:
        return _parse_json_map(v)

    # ── derived helpers ──────────────────────────────────────────────────────
    @property
    def is_dev(self) -> bool:
        return self.app_env.lower() in {"dev", "development", "local", "test"}

    @property
    def has_ai_key(self) -> bool:
        return bool(self.nanogpt_api_key.strip())

    @property
    def uses_postgres(self) -> bool:
        return self.database_url.startswith("postgresql")

    def task_models(self, task: AITask | str) -> list[str]:
        """Ordered model chain for a task: explicit routing first, then the
        dedicated setting + its fallback list."""
        key = task.value if isinstance(task, AITask) else str(task)
        routed = self.model_routing.get(key)
        if routed:
            return [m.strip() for m in routed if str(m).strip()]
        primary = {
            AITask.CONSULT.value: (self.model_consult, self.fallback_consult),
            AITask.COACH.value: (self.model_coach, self.fallback_coach),
            AITask.PLAN.value: (self.model_plan, self.fallback_coach),
            AITask.REPORT.value: (self.model_coach, self.fallback_coach),
            AITask.SUMMARY.value: (self.model_summary, self.fallback_consult),
            AITask.VISION_FOOD.value: (self.model_vision, self.fallback_vision),
            AITask.VISION_PROGRESS.value: (self.model_vision, self.fallback_vision),
            AITask.SAFETY.value: (self.model_summary, self.fallback_consult),
            AITask.BOXING.value: (self.model_coach, self.fallback_coach),
            AITask.STT.value: (self.model_stt, self.fallback_stt),
        }.get(key, (self.model_consult, self.fallback_consult))
        return [primary[0], *primary[1]]

    def price_of(self, model: str) -> tuple[float, float]:
        """(input, output) USD per 1M tokens for ``model``."""
        for candidate in (model, model.split(":")[0]):
            if candidate in self.model_pricing:
                row = self.model_pricing[candidate]
                return float(row[0]), float(row[1])
        return float(self.default_pricing[0]), float(self.default_pricing[1])


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
