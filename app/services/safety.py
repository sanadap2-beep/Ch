"""Safety screening (spec §2.5 + §5.6).

Two layers, defence in depth:

1. **Static rules** — deterministic, works offline, always applied. Maps a
   reported injury/condition to restricted movement patterns and safe swaps,
   so even if the AI is unavailable the plan is still safe.
2. **AI screen** — reads the user's free-text answer and extracts structured
   conditions/red flags (cheap model, JSON output).

Anything flagged red sets ``user.requires_medical_clearance`` and makes the
coach prepend a medical-referral line to intense prescriptions.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from app.ai.service import AIService
from app.constants import RED_FLAG_CONDITIONS, InjuryArea
from app.db.models import User
from app.db.repositories import Repos

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class SafetyScreen:
    red_flag: bool = False
    conditions: list[str] = field(default_factory=list)
    restricted: list[str] = field(default_factory=list)
    alternatives: list[str] = field(default_factory=list)
    referral: str = ""
    notes: str = ""
    source: str = "static"

    def merge(self, other: SafetyScreen) -> SafetyScreen:
        return SafetyScreen(
            red_flag=self.red_flag or other.red_flag,
            conditions=list(dict.fromkeys([*self.conditions, *other.conditions])),
            restricted=list(dict.fromkeys([*self.restricted, *other.restricted])),
            alternatives=list(dict.fromkeys([*self.alternatives, *other.alternatives])),
            referral=self.referral or other.referral,
            notes=" | ".join(x for x in (self.notes, other.notes) if x),
            source=f"{self.source}+{other.source}",
        )


# ── static rule tables ──────────────────────────────────────────────────────
RULES: dict[str, dict[str, Any]] = {
    InjuryArea.KNEE.value: {
        "restricted": ["قرفصاء عميقة تحت 90°", "طعنات (Lunges) بحمل ثقيل", "قفز على الصندوق", "جري على أرض صلبة"],
        "alternatives": ["Leg press بمدى محدود", "جسر الورك (Hip bridge)", "دراجة ثابتة", "سباحة", "Step-up منخفض"],
        "referral_ar": "إذا كان الألم حادًا أو مع تورم/عدم ثبات، راجع أخصائي علاج طبيعي قبل أي حمل على الركبة.",
        "referral_en": "If pain is sharp or there is swelling/instability, see a physiotherapist before loading the knee.",
    },
    InjuryArea.LOWER_BACK.value: {
        "restricted": ["رفعة ميتة ثقيلة", "Good mornings", "تمارين بطن برفع الجذع الكامل (Sit-ups)", "ضغط فوق الرأس واقفًا"],
        "alternatives": ["Bird dog", "Hip hinge بعصا للتوجيه", "تجديف مسنود الصدر", "بلانك", "سكوات بصندوق"],
        "referral_ar": "الانزلاق الغضروفي أو الألم الممتد للساق يستوجب تقييمًا طبيًا قبل أي حمل محوري.",
        "referral_en": "A herniated disc or pain radiating down the leg needs medical clearance before axial loading.",
    },
    InjuryArea.SHOULDER.value: {
        "restricted": ["ضغط فوق الرأس", "Upright rows", "Dips", "أي سحب خلف الرقبة", "بنش بمسافة واسعة جدًا"],
        "alternatives": ["Landmine press", "ضغط بقبضة محايدة (دمبل)", "Face pulls", "تجديف منخفض", "تمارين الكفة المدورة"],
        "referral_ar": "ألم الكتف مع رفع الذراع فوق الرأس يحتاج تقييمًا (كفة مدورة/اصطدام) قبل الضغط العلوي.",
        "referral_en": "Shoulder pain with overhead reach needs assessment (rotator cuff/impingement) before pressing.",
    },
    InjuryArea.NECK.value: {
        "restricted": ["Shrugs ثقيلة", "حمل فوق الرأس", "وقوف على الرأس", "أي ضغط مباشر على الرقبة"],
        "alternatives": ["تقوية رقبة إيزومترية خفيفة", "تجديف", "تمارين وضعية (Posture)"],
        "referral_ar": "خدر أو تنميل في الذراعين مع ألم الرقبة = راجع طبيبًا فورًا.",
        "referral_en": "Numbness or tingling in the arms with neck pain = see a doctor immediately.",
    },
    InjuryArea.ANKLE.value: {
        "restricted": ["قفز", "جري", "تمارين باتزان على سطح غير مستقر بدون إشراف"],
        "alternatives": ["دراجة", "سباحة", "تقوية ربلة", "تمارين توازن تدريجية"],
        "referral_ar": "التواء متكرر أو عدم ثبات يحتاج تأهيلًا قبل أي قفز.",
        "referral_en": "Recurrent sprains or instability need rehab before any jumping.",
    },
    InjuryArea.WRIST.value: {
        "restricted": ["ضغط أرضي على راحة اليد", "Front squat", "حمل ثقيل بقبضة مكسورة"],
        "alternatives": ["Push-up على مقابض أو قبضة", "قبضة محايدة في كل التمارين", "تقوية رسغ تدريجية"],
        "referral_ar": "ألم الرسغ المستمر مع تورم يحتاج تقييمًا لاستبعاد كسر إجهادي.",
        "referral_en": "Persistent wrist pain with swelling needs imaging to rule out a stress fracture.",
    },
    InjuryArea.ELBOW.value: {
        "restricted": ["Curls ثقيلة", "Triceps extensions فوق الرأس", "قبض قوي متكرر"],
        "alternatives": ["عمل إيزومتري", "قبضة محايدة (Hammer)", "أحزمة رفع للأحمال الثقيلة"],
        "referral_ar": "ألم المرفق الداخلي/الخارجي (Golf/Tennis elbow) يتحسن بتعديل الحمل لا بإيقافه كليًا — راجع أخصائيًا.",
        "referral_en": "Medial/lateral elbow pain improves with load management, not full rest — see a specialist.",
    },
    InjuryArea.HIP.value: {
        "restricted": ["قرفصاء عميقة", "تقريب شديد للأرجل", "رفع ثقيل من الأرض"],
        "alternatives": ["Hip thrust", "سكوات بمدى مريح", "تقوية مقربات تدريجية"],
        "referral_ar": "ألم الفخذ الداخلي أو طقطقة مؤلمة تحتاج تقييمًا للحق المفصلي.",
        "referral_en": "Groin pain or painful clicking needs assessment of the hip joint.",
    },
    InjuryArea.CHEST_HEART.value: {
        "restricted": ["أي تدريب عالي الشدة (HIIT)", "أحمال قصوى (1RM)", "حبس النفس تحت حمل (Valsalva)"],
        "alternatives": ["مشي", "عمل قلبي بمنطقة 2", "مقاومة خفيفة بتكرارات عالية"],
        "referral_ar": "🔴 حالة قلبية/صدرية = لا تبدأ أي برنامج قبل موافقة طبيب خطية.",
        "referral_en": "🔴 Cardiac/chest condition = do not start any programme without a doctor's written clearance.",
        "red_flag": True,
    },
}

# Free-text keyword → area (works for Arabic and English answers)
KEYWORDS: dict[str, list[str]] = {
    InjuryArea.KNEE.value: ["ركب", "ركبة", "knee", "meniscus", "غضروف الركبة", "acl", "الرباط الصليبي"],
    InjuryArea.LOWER_BACK.value: ["ظهر", "قطني", "back", "لومبر", "lumb", "انزلاق", "herniat", "ديسك", "disc", "sciatica", "عرق النسا"],
    InjuryArea.SHOULDER.value: ["كتف", "shoulder", "rotator", "كفة"],
    InjuryArea.NECK.value: ["رقب", "neck", "cervic", "عنق"],
    InjuryArea.ANKLE.value: ["كاحل", "ankle", "قدم", "foot", "التواء"],
    InjuryArea.WRIST.value: ["رسغ", "معصم", "wrist", "يد"],
    InjuryArea.ELBOW.value: ["مرفق", "elbow", "كوع"],
    InjuryArea.HIP.value: ["ورك", "hip", "حوض", "pelv"],
    InjuryArea.CHEST_HEART.value: ["قلب", "heart", "cardiac", "صدر", "chest", "ضغط", "pressure", "hypertens"],
}

RED_FLAG_KEYWORDS: dict[str, list[str]] = {
    "heart_disease": ["قلب", "heart", "cardiac", "ذبحة", "angina", "جلطة", "infarct"],
    "high_blood_pressure_uncontrolled": ["ضغط", "hypertens", "blood pressure"],
    "diabetes_type1": ["سكري نوع 1", "type 1", "type1", "انسولين", "insulin"],
    "recent_surgery": ["عملية", "جراحة", "surgery", "operation"],
    "pregnancy": ["حامل", "حمل", "pregnan"],
    "herniated_disc": ["انزلاق", "herniat", "ديسك", "disc"],
    "osteoporosis": ["هشاشة", "osteopor"],
    "kidney_disease": ["كلى", "كلية", "kidney", "renal"],
    "epilepsy": ["صرع", "epilep", "seizure"],
    "severe_asthma": ["ربو", "asthma", "ضيق تنفس"],
}

NEGATIVE_WORDS = [
    "لا شيء", "لاشيء", "ما عندي", "ماكو", "مافي", "لا يوجد", "لا", "none", "no", "nothing",
    "لا اصابات", "لا إصابات", "n/a", "لاشئ", "لا شئ", "سليم", "healthy", "-", "0",
]


def _is_negative(text: str) -> bool:
    cleaned = re.sub(r"[\s.,،!؟?]+", " ", (text or "").strip().lower())
    if not cleaned:
        return True
    if len(cleaned) <= 3:
        return cleaned in {"لا", "no", "-", "0", "نعم"} or True
    return any(word in cleaned for word in NEGATIVE_WORDS) and len(cleaned) < 30


def detect_areas(text: str) -> list[str]:
    lowered = (text or "").lower()
    found: list[str] = []
    for area, words in KEYWORDS.items():
        if any(word in lowered for word in words):
            found.append(area)
    return found


def detect_red_flags(text: str) -> list[str]:
    lowered = (text or "").lower()
    return [condition for condition, words in RED_FLAG_KEYWORDS.items() if any(w in lowered for w in words)]


def static_screen(text: str, lang: str = "ar") -> SafetyScreen:
    """Deterministic screen — always available, no AI call."""
    if _is_negative(text):
        return SafetyScreen(source="static")
    areas = detect_areas(text)
    flags = detect_red_flags(text)
    restricted: list[str] = []
    alternatives: list[str] = []
    referrals: list[str] = []
    for area in areas:
        rule = RULES.get(area)
        if not rule:
            continue
        restricted.extend(rule["restricted"])
        alternatives.extend(rule["alternatives"])
        referrals.append(rule["referral_ar"] if lang.startswith("ar") else rule["referral_en"])
        if rule.get("red_flag"):
            flags.append("chest_heart")
    red_flag = bool(set(flags) & RED_FLAG_CONDITIONS)
    if red_flag and lang.startswith("ar"):
        referrals.insert(0, "🔴 حالتك تستوجب موافقة طبية قبل بدء أي برنامج تدريبي.")
    elif red_flag:
        referrals.insert(0, "🔴 Your condition requires medical clearance before starting any programme.")
    return SafetyScreen(
        red_flag=red_flag,
        conditions=flags,
        restricted=list(dict.fromkeys(restricted)),
        alternatives=list(dict.fromkeys(alternatives)),
        referral=" ".join(dict.fromkeys(referrals)),
        notes=text[:500],
        source="static",
    )


class SafetyService:
    def __init__(self, ai: AIService, repos: Repos) -> None:
        self.ai = ai
        self.repos = repos

    async def screen(self, user: User | None, text: str, lang: str = "ar") -> SafetyScreen:
        """Static screen + AI enrichment (AI failure never blocks onboarding)."""
        base = static_screen(text, lang)
        if not text.strip() or _is_negative(text):
            return base
        try:
            outcome = await self.ai.screening(text, lang=lang, user_id=getattr(user, "id", None))
            data = outcome.data or {}
            ai_screen = SafetyScreen(
                red_flag=bool(data.get("red_flag", False)),
                conditions=[str(c) for c in (data.get("conditions") or [])],
                restricted=[str(c) for c in (data.get("restricted_movements") or [])],
                alternatives=[str(c) for c in (data.get("safe_alternatives") or [])],
                referral=str(data.get("medical_referral") or ""),
                notes=str(data.get("notes") or ""),
                source="ai",
            )
            return base.merge(ai_screen)
        except Exception as exc:  # noqa: BLE001 — fall back to the static rules
            logger.info("AI safety screen unavailable, using static rules: %s", exc)
            return base

    async def apply_to_user(self, user: User, screen: SafetyScreen) -> User:
        """Persist the screening result onto the profile."""
        conditions = list(dict.fromkeys([*(user.health_conditions or []), *screen.conditions]))
        user.health_conditions = conditions
        user.requires_medical_clearance = screen.red_flag or bool(
            set(conditions) & RED_FLAG_CONDITIONS
        )
        if screen.notes:
            user.medical_notes = screen.notes[:2000]
        injuries: list[dict[str, Any]] = list(user.injuries or [])
        existing_areas = {str(i.get("area")) for i in injuries if isinstance(i, dict)}
        for area in detect_areas(screen.notes or ""):
            if area not in existing_areas:
                injuries.append({"area": area, "note": (screen.notes or "")[:200], "severity": "unknown"})
        user.injuries = injuries
        preferences = dict(user.preferences or {})
        preferences["restricted_movements"] = screen.restricted
        preferences["safe_alternatives"] = screen.alternatives
        preferences["medical_referral"] = screen.referral
        user.preferences = preferences
        from app.db.models import utcnow

        user.safety_warning_shown_at = utcnow()
        await self.repos.session.flush()
        return user

    def restricted_for(self, user: User) -> list[str]:
        prefs = user.preferences or {}
        return [str(x) for x in (prefs.get("restricted_movements") or [])]

    def warning_line(self, user: User, lang: str = "ar") -> str | None:
        """Short medical-referral line the coach must show before hard sessions."""
        if not (user.requires_medical_clearance or user.injuries or user.health_conditions):
            return None
        prefs = user.preferences or {}
        referral = prefs.get("medical_referral")
        if referral:
            return str(referral)
        if lang.startswith("ar"):
            return "⚠️ عندك حالة صحية مسجلة — تجنّب الألم، وأوقف التمرين وراجع طبيبًا إذا زاد."
        return "⚠️ You have a recorded health condition — avoid pain, stop and see a doctor if it worsens."

    def plan_safety_notes(self, user: User, lang: str = "ar") -> list[str]:
        notes: list[str] = []
        restricted = self.restricted_for(user)
        if restricted:
            label = "تمارين مستبعدة لك: " if lang.startswith("ar") else "Excluded for you: "
            notes.append(label + "، ".join(restricted[:8]))
        alternatives = [str(x) for x in ((user.preferences or {}).get("safe_alternatives") or [])]
        if alternatives:
            label = "بدائل آمنة: " if lang.startswith("ar") else "Safe alternatives: "
            notes.append(label + "، ".join(alternatives[:8]))
        line = self.warning_line(user, lang)
        if line:
            notes.append(line)
        return notes
