"""Data-driven goal registry.

The spec requires a bot that is **not** built around a single objective and that
can gain new goals/coaches later without refactoring.  So every goal is a
:class:`GoalProfile` record: its calorie delta, macro ratios, expected rate of
change, training flavour and prompt hints all live in data.

Adding a goal = add a :class:`Goal` member + one profile here.  Nothing else
changes: metabolism, plan generation, reports and the UI all read this registry.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.constants import Goal


@dataclass(frozen=True, slots=True)
class GoalProfile:
    key: Goal
    label_ar: str
    label_en: str
    # calorie adjustment relative to TDEE (negative = deficit)
    calorie_delta_pct: float
    protein_g_per_kg: float
    fat_g_per_kg: float
    carb_style: str = "moderate"          # low | moderate | high
    # expected weekly body-weight change (kg) — used to detect stalls & overshoot
    expected_rate_kg_week: tuple[float, float] = (-0.2, 0.2)
    stall_direction: str = "hold"          # decrease | increase | hold
    training_flavour_ar: str = ""
    training_flavour_en: str = ""
    plan_kind: str = "training"            # training | boxing | nutrition | flexibility
    default_weeks: int = 4
    wants_target_weight: bool = True
    extra: dict[str, Any] = field(default_factory=dict)

    def describe(self, lang: str = "ar") -> str:
        return self.label_ar if lang.startswith("ar") else self.label_en

    def flavour(self, lang: str = "ar") -> str:
        return self.training_flavour_ar if lang.startswith("ar") else self.training_flavour_en


GOALS: dict[str, GoalProfile] = {
    Goal.CUT.value: GoalProfile(
        key=Goal.CUT,
        label_ar="تنشيف (فقدان دهون)",
        label_en="Cut (fat loss)",
        calorie_delta_pct=-0.20,
        protein_g_per_kg=2.2,
        fat_g_per_kg=0.8,
        carb_style="moderate",
        expected_rate_kg_week=(-1.0, -0.3),
        stall_direction="decrease",
        training_flavour_ar=(
            "حافظ على شدة الأوزان وحجم معتدل للحفاظ على العضل، "
            "وأضف عملًا قلبيًا منخفض الشدة (مشي/درجات) أو HIIT قصير 2-3 مرات أسبوعيًا."
        ),
        training_flavour_en=(
            "Keep lifting heavy at moderate volume to retain muscle; add low-intensity cardio "
            "or short HIIT 2-3×/week."
        ),
    ),
    Goal.BULK.value: GoalProfile(
        key=Goal.BULK,
        label_ar="تضخيم (بناء عضل)",
        label_en="Bulk (muscle gain)",
        calorie_delta_pct=0.10,
        protein_g_per_kg=1.8,
        fat_g_per_kg=0.9,
        carb_style="high",
        expected_rate_kg_week=(0.2, 0.6),
        stall_direction="increase",
        training_flavour_ar=(
            "تركيز على التدرج بالحمل (Progressive Overload) في التمارين المركبة، "
            "4-5 أيام أسبوعيًا، مع حجم كافٍ لكل مجموعة عضلية (10-20 مجموعة فعالة أسبوعيًا)."
        ),
        training_flavour_en=(
            "Progressive overload on compound lifts, 4-5 days/week, 10-20 hard sets per muscle group weekly."
        ),
    ),
    Goal.RECOMP.value: GoalProfile(
        key=Goal.RECOMP,
        label_ar="إعادة تركيب الجسم (تنشيط عام)",
        label_en="Body recomposition",
        calorie_delta_pct=-0.05,
        protein_g_per_kg=2.0,
        fat_g_per_kg=0.85,
        carb_style="moderate",
        expected_rate_kg_week=(-0.3, 0.3),
        stall_direction="hold",
        training_flavour_ar=(
            "مزيج قوة + تضخيم: تمارين مركبة ثقيلة بداية الأسبوع، وعزل وحجم أعلى نهايته، "
            "مع كارديو خفيف للحفاظ على حساسية الإنسولين."
        ),
        training_flavour_en=(
            "Strength + hypertrophy blend: heavy compounds early week, higher-volume isolation later, "
            "light cardio to keep insulin sensitivity."
        ),
    ),
    Goal.GENERAL_FITNESS.value: GoalProfile(
        key=Goal.GENERAL_FITNESS,
        label_ar="تحسين اللياقة العامة",
        label_en="General fitness",
        calorie_delta_pct=0.0,
        protein_g_per_kg=1.6,
        fat_g_per_kg=0.9,
        carb_style="moderate",
        expected_rate_kg_week=(-0.4, 0.4),
        stall_direction="hold",
        training_flavour_ar=(
            "تدريب متوازن: يومان قوة للجسم كامل، يومان عمل قلبي (منطقة 2)، يوم حركة/مرونة، "
            "ويومان راحة نشطة."
        ),
        training_flavour_en=(
            "Balanced: 2 full-body strength days, 2 zone-2 cardio days, 1 mobility day, 2 active-rest days."
        ),
    ),
    Goal.STRENGTH.value: GoalProfile(
        key=Goal.STRENGTH,
        label_ar="زيادة القوة",
        label_en="Strength gain",
        calorie_delta_pct=0.05,
        protein_g_per_kg=1.9,
        fat_g_per_kg=1.0,
        carb_style="high",
        expected_rate_kg_week=(-0.2, 0.4),
        stall_direction="increase",
        training_flavour_ar=(
            "عمل على الرفع الرئيسي (سكوات/بنش/ديدليفت/أوفرهيد) 2-3 مرات أسبوعيًا، "
            "3-5 تكرارات بشدة 80-90% من 1RM مع راحة 3-5 دقائق، وتمارين مساعدة بحجم معتدل."
        ),
        training_flavour_en=(
            "Main lifts 2-3×/week at 3-5 reps / 80-90% 1RM with 3-5 min rest, plus moderate-volume accessories."
        ),
    ),
    Goal.ENDURANCE.value: GoalProfile(
        key=Goal.ENDURANCE,
        label_ar="تحمل ولياقة قلبية",
        label_en="Endurance & cardio",
        calorie_delta_pct=0.0,
        protein_g_per_kg=1.6,
        fat_g_per_kg=0.9,
        carb_style="high",
        expected_rate_kg_week=(-0.5, 0.2),
        stall_direction="hold",
        training_flavour_ar=(
            "توزيع 80/20: معظم الحجم بشدة منخفضة (منطقة 2) + جلسة فواصل عالية الشدة أسبوعيًا، "
            "مع تقوية مقاومة للإصابة (أرداف/ربلة/جذع)."
        ),
        training_flavour_en=(
            "80/20 polarised: most volume at zone 2 + one weekly high-intensity interval session, "
            "plus injury-resistance strength work."
        ),
    ),
    Goal.BOXING.value: GoalProfile(
        key=Goal.BOXING,
        label_ar="تعلم الملاكمة من الصفر",
        label_en="Learn boxing from scratch",
        calorie_delta_pct=-0.05,
        protein_g_per_kg=1.8,
        fat_g_per_kg=0.9,
        carb_style="high",
        expected_rate_kg_week=(-0.6, 0.2),
        stall_direction="hold",
        plan_kind="boxing",
        default_weeks=6,
        training_flavour_ar=(
            "برنامج مرحلي: وقفة وحركة أقدام → اللكمات الأساسية → الدفاع → تركيبات → عمل على الكيس/الظل → جولات. "
            "مع تكييف خاص (حبل قفز، فواصل 2-3 دقائق) وتقوية للكمة والرسغ بدون معدات."
        ),
        training_flavour_en=(
            "Phased: stance & footwork → basic punches → defence → combinations → bag/shadow work → rounds, "
            "with boxing conditioning and equipment-free punch/wrist strength."
        ),
    ),
    Goal.FLEXIBILITY.value: GoalProfile(
        key=Goal.FLEXIBILITY,
        label_ar="مرونة واستطالة",
        label_en="Flexibility & mobility",
        calorie_delta_pct=0.0,
        protein_g_per_kg=1.5,
        fat_g_per_kg=0.9,
        carb_style="moderate",
        expected_rate_kg_week=(-0.3, 0.3),
        stall_direction="hold",
        plan_kind="flexibility",
        training_flavour_ar=(
            "استطالة ديناميكية قبل النشاط وثابتة بعده، عمل مرونة يومي قصير (10-15 دقيقة)، "
            "ومرونة نشطة (PNF) 3 مرات أسبوعيًا للوركين والأكتاف والعمود الفقري."
        ),
        training_flavour_en=(
            "Dynamic stretching pre-work, static post-work, short daily mobility (10-15 min) and PNF 3×/week "
            "for hips, shoulders and spine."
        ),
    ),
    Goal.HEIGHT.value: GoalProfile(
        key=Goal.HEIGHT,
        label_ar="زيادة الطول (عمر صغير)",
        label_en="Height support (young age)",
        calorie_delta_pct=0.05,
        protein_g_per_kg=1.7,
        fat_g_per_kg=1.0,
        carb_style="high",
        expected_rate_kg_week=(-0.2, 0.5),
        stall_direction="hold",
        plan_kind="flexibility",
        wants_target_weight=False,
        training_flavour_ar=(
            "الصدق أولًا: الطول يتحدد وراثيًا ولوحات النمو، ولا يوجد تمرين يزيد العظم طولًا بعد إغلاقها. "
            "ما يمكن فعله فعليًا: تعظيم الإفراز الطبيعي لهرمون النمو (نوم 8-10 ساعات، تدريب عالي الشدة، "
            "تغذية كافية بالبروتين والكالسيوم وفيتامين د والزنك)، وتمارين تعليق واستطالة وتمديد للعمود الفقري "
            "تحسّن الوقفة وتُظهر 1-3 سم من الطول المخفي، ورياضات قفز (سلة/سباحة/حبل)."
        ),
        training_flavour_en=(
            "Be honest: height is genetic and driven by growth plates; no exercise lengthens bone after they close. "
            "What actually helps: maximise natural GH (8-10 h sleep, high-intensity training, enough protein, "
            "calcium, vitamin D, zinc), hanging/spinal-decompression and stretching work that improves posture "
            "(reveals 1-3 cm of hidden height), and jumping sports."
        ),
    ),
    Goal.WEIGHT_GAIN.value: GoalProfile(
        key=Goal.WEIGHT_GAIN,
        label_ar="زيادة الوزن (نحافة)",
        label_en="Weight gain (underweight)",
        calorie_delta_pct=0.15,
        protein_g_per_kg=1.8,
        fat_g_per_kg=1.1,
        carb_style="high",
        expected_rate_kg_week=(0.2, 0.7),
        stall_direction="increase",
        training_flavour_ar=(
            "قوة وتضخيم 3-4 أيام أسبوعيًا بتمارين مركبة، مع تقليل الكارديو للحفاظ على السعرات، "
            "وأكل كثيف السعرات (مكسرات، زيوت، عصائر سعرات) لمن يشبع بسرعة."
        ),
        training_flavour_en=(
            "Strength/hypertrophy 3-4 days on compounds, minimal cardio to protect calories, "
            "calorie-dense foods and shakes for early satiety."
        ),
    ),
    Goal.GENERAL_HEALTH.value: GoalProfile(
        key=Goal.GENERAL_HEALTH,
        label_ar="صحة عامة",
        label_en="General health",
        calorie_delta_pct=0.0,
        protein_g_per_kg=1.4,
        fat_g_per_kg=0.9,
        carb_style="moderate",
        expected_rate_kg_week=(-0.3, 0.3),
        stall_direction="hold",
        training_flavour_ar=(
            "الحد الأدنى الفعّال: 150 دقيقة عمل قلبي معتدل أسبوعيًا + جلستان مقاومة لكل الجسم + "
            "مرونة وتوازن، مع تركيز على النوم والضغط النفسي."
        ),
        training_flavour_en=(
            "Minimum effective dose: 150 min moderate cardio/week + 2 full-body resistance sessions + "
            "mobility/balance, with sleep and stress focus."
        ),
    ),
    Goal.CUSTOM.value: GoalProfile(
        key=Goal.CUSTOM,
        label_ar="هدف مخصص",
        label_en="Custom goal",
        calorie_delta_pct=0.0,
        protein_g_per_kg=1.8,
        fat_g_per_kg=0.9,
        carb_style="moderate",
        expected_rate_kg_week=(-0.5, 0.5),
        stall_direction="hold",
        training_flavour_ar=(
            "الهدف مكتوب بصيغة المستخدم — اقرأ وصفه بدقة وابنِ الخطة عليه، واسأل سؤالًا واحدًا "
            "محددًا إذا كان الوصف غامضًا."
        ),
        training_flavour_en=(
            "The goal is described in the user's own words — read it carefully and build around it; "
            "ask one specific question if it is ambiguous."
        ),
    ),
}


def get_goal(goal: str | Goal | None) -> GoalProfile:
    """Look up a profile, falling back to general health for unknown values."""
    if goal is None:
        return GOALS[Goal.GENERAL_HEALTH.value]
    key = goal.value if isinstance(goal, Goal) else str(goal)
    return GOALS.get(key, GOALS[Goal.GENERAL_HEALTH.value])


def all_goals(lang: str = "ar") -> list[dict[str, str]]:
    return [
        {"key": p.key.value, "label": p.describe(lang), "flavour": p.flavour(lang)}
        for p in GOALS.values()
    ]


def register_goal(profile: GoalProfile) -> None:
    """Runtime extension hook — lets a future "new coach" be added without a
    schema change (e.g. loaded from ``app_settings``)."""
    GOALS[profile.key.value if isinstance(profile.key, Goal) else str(profile.key)] = profile
