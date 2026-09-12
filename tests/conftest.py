"""Shared fixtures: in-process SQLite database, service container, offline AI.

No network is used anywhere in the suite — the NanoGPT client is monkeypatched
with deterministic, schema-valid payloads so the whole pipeline (prompt → parse
→ persist → charge → render) is exercised exactly as in production.
"""
from __future__ import annotations

import json
import os
from typing import Any

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("BOT_TOKEN", "123456789:TEST-token-for-unit-tests")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./storage/_pytest.db")
os.environ.setdefault("STORAGE_BACKEND", "local")
os.environ.setdefault("WEB_SEARCH_ENABLED", "false")
os.environ.setdefault("NANOGPT_API_KEY", "test-key")

from datetime import UTC

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402

from app.ai.client import AIResult, NanoGPTClient, Usage  # noqa: E402
from app.config import settings  # noqa: E402
from app.constants import ActivityLevel, BudgetLevel, Equipment, FoodStyle, Gender, Goal  # noqa: E402
from app.db.base import dispose_engine, get_session_factory, init_db, reset_engine_for_tests  # noqa: E402
from app.db.models import User  # noqa: E402
from app.services.context import Services  # noqa: E402

# ═══════════════════════════════════════════════════════════════════════════
#  canned AI payloads (schema-valid)
# ═══════════════════════════════════════════════════════════════════════════
CANNED: dict[str, Any] = {
    "consultation_answer": {
        "answer": "<b>خطة سريعة لحرق 300 سعرة</b>\n• ابدأ بتمارين مركّبة 3 جولات.",
        "exercises": [
            {
                "name": "بيربي",
                "how_to": "نزول للارض ثم قفزة مع رفع اليدين.",
                "sets_reps": "3×12",
                "target_muscle": "كامل الجسم",
                "calories_estimate": 150,
                "video_query": "burpees proper form",
                "contraindicated_for": ["knee"],
            }
        ],
        "meals": [
            {"name": "شوفان بالحليب", "calories": 320, "protein_g": 14.0, "carbs_g": 48.0,
             "fats_g": 7.0, "note": "فطور مشبع ورخيص"}
        ],
        "follow_up_question": "كم يوم بالأسبوع تقدر تتمرن؟",
        "safety_note": None,
        "estimated_total_calories": 300,
    },
    "food_analysis": {
        "meal_name": "شاورما دجاج مع بطاطا",
        "items": [
            {"name": "شاورما دجاج", "portion_g": 220.0, "cooking_method": "مشوي", "calories": 480,
             "protein_g": 38.0, "carbs_g": 32.0, "fats_g": 18.0, "confidence": 0.82},
            {"name": "بطاطا مقلية", "portion_g": 120.0, "cooking_method": "مقلي", "calories": 320,
             "protein_g": 4.0, "carbs_g": 42.0, "fats_g": 15.0, "confidence": 0.7},
        ],
        "totals": {"calories": 800, "protein_g": 42.0, "carbs_g": 74.0, "fats_g": 33.0},
        "assumptions": ["الخبز متوسط الحجم", "زيت القليل في الشاورما"],
        "clarifications": [],
        "confidence": 0.78,
        "is_food": True,
        "notes": None,
    },
    "food_analysis_ambiguous": None,   # filled per-test
    "progress_photo_analysis": {
        "is_body_photo": True,
        "estimated_body_fat_pct_range": "18-22%",
        "muscle_development": "متوسط",
        "posture_issues": ["تقوس بسيط في الكتف"],
        "symmetry_notes": None,
        "visible_changes": ["تحسن في محيط الخصر"],
        "weight_estimate_kg_range": "80-84",
        "injury_or_pain_signs": [],
        "photo_quality_notes": None,
        "recommendations": ["أضف تمارين سحب للظهر"],
        "confidence": 0.6,
    },
    "workout_plan": {
        "title": "برنامج بيت 4 أسابيع",
        "goal": "cut",
        "equipment": "home_no_equipment",
        "weeks_total": 1,
        "weeks": [
            {
                "week_index": 1,
                "title": "أساس",
                "focus": "تكييف",
                "progression": "زد جولة كل أسبوع",
                "days": [
                    {
                        "day_index": 1, "day_name": "السبت", "focus": "علوي", "is_rest": False,
                        "estimated_minutes": 35, "estimated_calories_burned": 260,
                        "warmup": ["5 دقائق مشي"], "cooldown": ["استطالة 5 دقائق"],
                        "exercises": [
                            {"name": "ضغط", "target_muscle": "صدر", "sets": 3, "reps": "10-15",
                             "rest_seconds": 60, "weight_hint": None, "cue": "شد البطن",
                             "video_url": None, "alternative_for": None}
                        ],
                        "notes": None,
                    },
                    {
                        "day_index": 2, "day_name": "الأحد", "focus": "راحة", "is_rest": True,
                        "estimated_minutes": 0, "estimated_calories_burned": 0,
                        "warmup": [], "cooldown": [], "exercises": [], "notes": "مشي خفيف",
                    },
                ],
            }
        ],
        "safety_notes": ["توقف عند الألم الحاد"],
        "restricted_exercises": [],
        "progression_rules": ["زد التكرارات قبل الجولات"],
        "metrics_to_track": ["الوزن", "محيط الخصر"],
    },
    "nutrition_plan": {
        "title": "وجبات اليوم",
        "daily_calories": 2100,
        "daily_protein_g": 150,
        "daily_carbs_g": 210,
        "daily_fats_g": 65,
        "meals_per_day": 3,
        "meals": [
            {"slot": "breakfast", "name": "فول مع خبز", "ingredients": [{"name": "فول", "grams": 200.0}],
             "calories": 420, "protein_g": 22.0, "carbs_g": 55.0, "fats_g": 10.0, "cost_tier": "cheap",
             "method": "سخّن الفول وأضف ليمون وكمون.", "swaps": ["حمص"]},
        ],
        "shopping_list": [{"item": "فول", "weekly_amount": "2 علبة", "cost_tier": "cheap"}],
        "budget_notes": ["البروتين من البيض والفول أرخص من اللحمة"],
        "hydration_ml": 3000,
        "supplement_notes": [],
    },
    "boxing_programme": {
        "title": "الملاكمة — المرحلة 1",
        "phase": 1,
        "goal": "تعلّم الأساسيات",
        "weeks_total": 1,
        "weeks": [
            {"week_index": 1, "title": "الأساسيات", "focus": "الوقفة واللكمات",
             "progression": "زد مدة الجولة", "days": []}
        ],
        "next_phase_preview": "المرحلة 2:组合ات",
        "safety_notes": ["لفّ يديك قبل الضرب على الكيس"],
        "gear_needed": [],
    },
    "safety_screen": {
        "red_flag": False,
        "conditions": ["knee"],
        "restricted_movements": ["قفز", "قرفصاء عميقة"],
        "safe_alternatives": ["دراجة ثابتة", "سباحة"],
        "medical_referral": "راجع أخصائي علاج طبيعي إذا زاد الألم",
        "notes": "إصابة ركبة مزمنة",
    },
    "day_recalculation": {
        "explanation": "فوّت الغدا، وزّعت المتبقي على وجبتين.",
        "new_remaining_calories": 900,
        "new_remaining_protein_g": 70.0,
        "new_remaining_carbs_g": 95.0,
        "new_remaining_fats_g": 28.0,
        "suggested_meals": [
            {"slot": "dinner", "name": "عدس مع رز", "ingredients": [{"name": "عدس", "grams": 150.0}],
             "calories": 520, "protein_g": 30.0, "carbs_g": 70.0, "fats_g": 8.0, "cost_tier": "cheap",
             "method": "اطبخ العدس مع الرز بقدرة واحدة.", "swaps": ["برغل"]}
        ],
        "warnings": [],
    },
}

PLAIN_COACH_REPLY = (
    "تمام، سجّلت وزنك. باقي لك 420 سعرة و30 جرام بروتين — "
    "اقترح وجبة عشاء خفيفة: 3 بيضات مع خبز أسمر وسلطة. وأشرب كاستين ماء قبل التمرين."
)
PLAIN_REPORT = (
    "<b>التقرير الأسبوعي</b>\n• الالتزام: 82%\n• الوزن: −0.8 كجم\n• التمارين: 4/5\n"
    "الأسبوع الجاي ركّز على البروتين في الفطور."
)
PLAIN_SUMMARY = "المستخدم ملتزم، ينقص 0.8 كجم أسبوعيًا، يحتاج بروتين أكثر في الفطور."


async def _fake_complete(
    self: NanoGPTClient,
    messages: list[dict[str, Any]],
    *,
    models: list[str],
    task: str = "consult",
    images: Any = None,
    json_schema: dict[str, Any] | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    web_search: bool = False,
    extra: dict[str, Any] | None = None,
    on_attempt: Any = None,
) -> AIResult:
    """Deterministic stand-in for the NanoGPT chat-completion endpoint."""
    payload: str
    if json_schema and json_schema.get("name") in CANNED and CANNED[json_schema["name"]] is not None:
        payload = json.dumps(CANNED[json_schema["name"]], ensure_ascii=False)
    elif task in {"report", "coach"} and not json_schema:
        payload = PLAIN_REPORT if task == "report" else PLAIN_COACH_REPLY
    elif task == "summary":
        payload = PLAIN_SUMMARY
    else:
        payload = PLAIN_COACH_REPLY

    result = AIResult(
        text=payload,
        model=models[0] if models else "test/model",
        usage=Usage(prompt_tokens=320, completion_tokens=480, total_tokens=800, cost_usd=0.0016),
        raw={"id": "test"},
    )
    if on_attempt is not None:
        await on_attempt(result.model, result, None)
    return result


async def _fake_transcribe(self, audio, *, filename="voice.ogg", models=None, language=None):  # noqa: ANN001
    return "وزني اليوم 81.4 وشربت كاستين ماء", Usage(0, 0, 0, 0.0004), (models or ["Whisper-Large-V3"])[0]


async def _fake_web_search(self, query, *, include_domains=None, max_results=5, provider=None, depth="standard"):  # noqa: ANN001
    return [{"url": "https://www.youtube.com/watch?v=test123", "title": query, "snippet": "demo"}]


@pytest.fixture(autouse=True)
def _detach_application_routers():
    """aiogram refuses to attach a router that already has a parent.

    Tests build a fresh Dispatcher per test (so FSM state cannot leak), which means
    the shared application routers must be detached again each time.
    """
    from app.handlers import routers

    for application_router in routers:
        application_router._parent_router = None      # noqa: SLF001
    yield
    for application_router in routers:
        application_router._parent_router = None      # noqa: SLF001


@pytest.fixture(autouse=True)
def offline_ai(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test runs against the canned AI, never the network."""
    monkeypatch.setattr(NanoGPTClient, "complete", _fake_complete)
    monkeypatch.setattr(NanoGPTClient, "transcribe", _fake_transcribe)
    monkeypatch.setattr(NanoGPTClient, "web_search", _fake_web_search)
    monkeypatch.setattr(settings, "web_search_enabled", False, raising=False)


# ═══════════════════════════════════════════════════════════════════════════
#  database / services
# ═══════════════════════════════════════════════════════════════════════════
@pytest_asyncio.fixture
async def session(tmp_path):
    url = f"sqlite+aiosqlite:///{tmp_path / 'test.db'}"
    reset_engine_for_tests(url)
    await init_db(create_schema=True)
    factory = get_session_factory()
    async with factory() as db_session:
        yield db_session
        await db_session.rollback()
    await dispose_engine()


@pytest_asyncio.fixture
async def services(session) -> Services:
    container = Services(session, None)
    yield container
    await container.close()


@pytest_asyncio.fixture
async def repos(services):
    return services.repos


# ═══════════════════════════════════════════════════════════════════════════
#  user factories
# ═══════════════════════════════════════════════════════════════════════════
def make_user(**overrides: Any) -> User:
    data: dict[str, Any] = {
        "tg_id": overrides.pop("tg_id", 424242),
        "username": "tester",
        "first_name": "Test",
        "display_name": "Tester",
        "language_code": "ar",
        "age": 28,
        "gender": Gender.MALE.value,
        "height_cm": 178.0,
        "weight_kg": 86.0,
        "target_weight_kg": 78.0,
        "activity_level": ActivityLevel.LIGHT.value,
        "goal": Goal.CUT.value,
        "equipment": Equipment.HOME_BASIC.value,
        "budget_level": BudgetLevel.LOW.value,
        "food_style": FoodStyle.LOCAL_POPULAR.value,
        "user_timezone": "UTC",
        "points_balance": 500,
        "referral_code": overrides.pop("referral_code", "TESTCODE"),
    }
    data.update(overrides)
    return User(**data)


@pytest_asyncio.fixture
async def user(session) -> User:
    """A persisted, onboarding-complete user with points."""
    from datetime import datetime

    person = make_user()
    person.onboarding_completed_at = datetime.now(UTC)
    person.target_calories = 2100
    person.target_protein_g = 160
    person.target_carbs_g = 200
    person.target_fats_g = 65
    person.water_target_ml = 3000
    person.bmr = 1850.0
    person.tdee = 2500.0
    person.privacy_consent_at = datetime.now(UTC)
    person.privacy_policy_version = "1.0"
    session.add(person)
    await session.commit()
    return person


@pytest_asyncio.fixture
async def new_user(session) -> User:
    """A brand-new user (no onboarding, no consent)."""
    person = make_user(tg_id=999000, referral_code="NEWCODE")
    session.add(person)
    await session.commit()
    return person


@pytest_asyncio.fixture
async def coach_user(session, user) -> User:
    from datetime import datetime, timedelta

    user.coach_access_until = datetime.now(UTC) + timedelta(days=30)
    await session.commit()
    return user


# ═══════════════════════════════════════════════════════════════════════════
#  Telegram-layer fixtures (real dispatcher, mocked transport)
# ═══════════════════════════════════════════════════════════════════════════
@pytest.fixture(autouse=True)
def _fresh_ai_router_cache():
    """Model-routing/budget overrides live in a process-wide cache — never leak them."""
    from app.ai import router as ai_router

    ai_router.invalidate_cache()
    yield
    ai_router.invalidate_cache()


@pytest_asyncio.fixture
async def tg(session) -> Any:
    """Bot + production dispatcher + mocked Telegram transport on the test database.

    Each test gets its own Dispatcher so FSM state cannot leak between tests; the
    shared application routers are detached again by ``_detach_application_routers``.
    """
    from types import SimpleNamespace

    from tests.telegram_mock import build_test_dispatcher, make_bot

    bot, api = make_bot()
    dp = build_test_dispatcher(bot)
    try:
        yield SimpleNamespace(bot=bot, api=api, dp=dp, session=session)
    finally:
        await bot.session.close()
