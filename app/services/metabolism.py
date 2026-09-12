"""Exercise science: BMR / TDEE / macros / hydration / automatic re-calibration.

Formulas
--------
* **BMR** — Mifflin-St Jeor (the most accurate general-purpose equation):
  ``10·kg + 6.25·cm − 5·age + 5`` (men) / ``− 161`` (women).
* **TDEE** — BMR × activity multiplier (Harris-Benedict scale).
* **Target calories** — TDEE adjusted by the goal's deficit/surplus percentage,
  then clamped by safety floors/ceilings from settings.
* **Macros** — protein g/kg (on *adjusted* body weight when body fat is likely
  high, so obese users are not over-prescribed protein), fats g/kg with a
  hormonal-safety floor, carbs fill the remaining calories.
* **Water** — 33 ml/kg baseline + training allowance.

Re-calibration
--------------
``recalibrate()`` implements the spec's rule *"weight flat for two weeks →
adjust calories automatically"*: it reads the measured trend, decides the
direction from the goal profile (a cut stalls downward, a bulk stalls upward),
applies a 150–250 kcal step and records *why* in ``user.plan_adjustments`` so
the coach can explain it and the admin can audit it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

from app.config import settings
from app.constants import (
    STALL_WEEKS_BEFORE_ADJUST,
    WATER_ML_PER_KG,
    Gender,
    Goal,
)
from app.db.models import User
from app.services.goal_registry import GoalProfile, get_goal


@dataclass(slots=True)
class NutritionTargets:
    bmr: float
    tdee: float
    calories: int
    protein_g: int
    carbs_g: int
    fats_g: int
    water_ml: int
    goal: str
    bmi: float | None = None
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "bmr": round(self.bmr),
            "tdee": round(self.tdee),
            "calories": self.calories,
            "protein_g": self.protein_g,
            "carbs_g": self.carbs_g,
            "fats_g": self.fats_g,
            "water_ml": self.water_ml,
            "goal": self.goal,
            "bmi": round(self.bmi, 1) if self.bmi else None,
            "notes": self.notes,
        }


# ═══════════════════════════════════════════════════════════════════════════
#  core equations
# ═══════════════════════════════════════════════════════════════════════════
def bmr_mifflin(weight_kg: float, height_cm: float, age: int, gender: str | None) -> float:
    """Mifflin-St Jeor resting metabolic rate."""
    base = 10.0 * float(weight_kg) + 6.25 * float(height_cm) - 5.0 * float(age)
    return round(base + (5.0 if gender != Gender.FEMALE else -161.0), 1)


def tdee_from(bmr: float, activity_level: str | None) -> float:
    from app.constants import ACTIVITY_MULTIPLIERS, ActivityLevel

    try:
        multiplier = ACTIVITY_MULTIPLIERS[ActivityLevel(activity_level)] if activity_level else 1.375
    except ValueError:
        multiplier = 1.375
    return round(bmr * multiplier, 1)


def bmi(weight_kg: float | None, height_cm: float | None) -> float | None:
    if not weight_kg or not height_cm:
        return None
    metres = float(height_cm) / 100.0
    if metres <= 0:
        return None
    return round(float(weight_kg) / (metres * metres), 1)


def bmi_label(value: float | None, lang: str = "ar") -> str:
    if value is None:
        return ""
    ar = lang.startswith("ar")
    if value < 18.5:
        return "نحافة" if ar else "underweight"
    if value < 25:
        return "طبيعي" if ar else "normal"
    if value < 30:
        return "زيادة وزن" if ar else "overweight"
    if value < 35:
        return "سمنة درجة أولى" if ar else "obesity class I"
    if value < 40:
        return "سمنة درجة ثانية" if ar else "obesity class II"
    return "سمنة مفرطة" if ar else "obesity class III"


def adjusted_body_weight(weight_kg: float, height_cm: float | None) -> float:
    """Protein prescription weight for people with a lot of fat mass.

    Prescribing 2.2 g/kg of *total* weight to a 130 kg person yields absurd
    numbers.  Above BMI 30 we blend lean-ish mass: ``ideal + 0.4·(actual − ideal)``.
    """
    if not height_cm:
        return weight_kg
    value = bmi(weight_kg, height_cm) or 0
    if value <= 30:
        return weight_kg
    ideal = 25.0 * (float(height_cm) / 100.0) ** 2
    return round(ideal + 0.4 * max(0.0, weight_kg - ideal), 1)


def water_target_ml(weight_kg: float | None, training_days: int = 4) -> int:
    if not weight_kg:
        return 2500
    base = float(weight_kg) * WATER_ML_PER_KG
    extra = 350.0 * (training_days / 7.0)
    return int(round((base + extra) / 50.0) * 50)


# ═══════════════════════════════════════════════════════════════════════════
#  plan computation
# ═══════════════════════════════════════════════════════════════════════════
def macro_split(
    calories: int,
    weight_kg: float,
    *,
    protein_g_per_kg: float,
    fat_g_per_kg: float,
    carb_style: str = "moderate",
    height_cm: float | None = None,
) -> tuple[int, int, int]:
    """Return (protein_g, carbs_g, fats_g) honouring the hormonal fat floor."""
    ref_weight = adjusted_body_weight(weight_kg, height_cm)
    protein_g = max(settings.min_protein_g, round(ref_weight * protein_g_per_kg))
    fats_g = max(round(weight_kg * 0.6), round(ref_weight * fat_g_per_kg))

    protein_kcal = protein_g * 4
    fat_kcal = fats_g * 9
    remaining = max(0, calories - protein_kcal - fat_kcal)

    # carb style shifts the remainder between carbs and fat
    if carb_style == "low":
        carbs_g = int(remaining * 0.20 / 4)
    elif carb_style == "high":
        carbs_g = int(remaining * 0.58 / 4)
    else:
        carbs_g = int(remaining * 0.42 / 4)

    # re-balance so the total matches the calorie target
    used = protein_kcal + fat_kcal + carbs_g * 4
    leftover = calories - used
    if leftover >= 200:
        carbs_g += int(leftover * 0.7 / 4)
        fats_g += int(leftover * 0.3 / 9)
    elif leftover <= -200:
        reduction = abs(leftover)
        carbs_g = max(40, carbs_g - int(reduction * 0.75 / 4))
        fats_g = max(round(weight_kg * 0.6), fats_g - int(reduction * 0.25 / 9))
    return int(protein_g), int(max(40, carbs_g)), int(fats_g)


def clamp_calories(calories: float, gender: str | None) -> tuple[int, list[str]]:
    notes: list[str] = []
    floor = settings.min_calories_female if gender == Gender.FEMALE else settings.min_calories_male
    value = int(round(calories))
    if value < floor:
        notes.append(
            f"تم رفع السعرات إلى حد الأمان {floor} (الهدف المحسوب كان {value})."
        )
        value = floor
    if value > settings.max_calories:
        notes.append(f"تم خفض السعرات إلى السقف {settings.max_calories}.")
        value = settings.max_calories
    return value, notes


def compute_targets(user: User, *, goal_override: str | None = None) -> NutritionTargets:
    """Full calculation from the user's profile (no AI, no I/O — pure math)."""
    goal_key = goal_override or user.goal or Goal.GENERAL_HEALTH.value
    profile: GoalProfile = get_goal(goal_key)

    weight = float(user.weight_kg or 0)
    height = float(user.height_cm or 0)
    age = int(user.age or 30)
    if weight <= 0 or height <= 0:
        return NutritionTargets(
            bmr=0, tdee=0, calories=0, protein_g=0, carbs_g=0, fats_g=0,
            water_ml=water_target_ml(None), goal=goal_key,
            notes=["الوزن والطول مطلوبان لحساب السعرات."],
        )

    bmr = bmr_mifflin(weight, height, age, user.gender)
    tdee = tdee_from(bmr, user.activity_level)
    raw = tdee * (1 + profile.calorie_delta_pct)

    # never let the deficit/surplus exceed the configured safety percentages
    if profile.calorie_delta_pct < 0:
        raw = max(raw, tdee * (1 - settings.max_deficit_pct))
    elif profile.calorie_delta_pct > 0:
        raw = min(raw, tdee * (1 + settings.max_surplus_pct))

    calories, notes = clamp_calories(raw, user.gender)
    protein_g, carbs_g, fats_g = macro_split(
        calories,
        weight,
        protein_g_per_kg=profile.protein_g_per_kg,
        fat_g_per_kg=profile.fat_g_per_kg,
        carb_style=profile.carb_style,
        height_cm=height,
    )
    value = bmi(weight, height)
    if value and value >= 30:
        notes.append(
            "تم حساب البروتين على وزن معدّل (وليس الوزن الكلي) لأن نسبة الدهون مرتفعة — أدق علميًا."
        )
    return NutritionTargets(
        bmr=bmr,
        tdee=tdee,
        calories=calories,
        protein_g=protein_g,
        carbs_g=carbs_g,
        fats_g=fats_g,
        water_ml=water_target_ml(weight),
        goal=goal_key,
        bmi=value,
        notes=notes,
    )


def apply_targets(user: User, targets: NutritionTargets, *, reason: str = "onboarding") -> User:
    user.bmr = targets.bmr
    user.tdee = targets.tdee
    user.target_calories = targets.calories
    user.target_protein_g = targets.protein_g
    user.target_carbs_g = targets.carbs_g
    user.target_fats_g = targets.fats_g
    user.water_target_ml = targets.water_ml
    from app.db.models import utcnow

    user.plan_updated_at = utcnow()
    adjustments = list(user.plan_adjustments or [])
    adjustments.append(
        {
            "date": date.today().isoformat(),
            "reason": reason,
            "calories": targets.calories,
            "protein_g": targets.protein_g,
            "carbs_g": targets.carbs_g,
            "fats_g": targets.fats_g,
            "notes": targets.notes,
        }
    )
    user.plan_adjustments = adjustments[-30:]
    return user


# ═══════════════════════════════════════════════════════════════════════════
#  automatic re-calibration
# ═══════════════════════════════════════════════════════════════════════════
@dataclass(slots=True)
class Recalibration:
    changed: bool
    old_calories: int
    new_calories: int
    delta: int
    reason: str
    reason_ar: str
    direction: str = "hold"
    per_week_kg: float = 0.0


def recalibrate(user: User, trend: dict[str, Any], *, weeks: int = STALL_WEEKS_BEFORE_ADJUST) -> Recalibration:
    """Decide whether today's numbers still work, and change them if not.

    ``trend`` comes from :meth:`TrackingRepository.weight_trend`.
    """
    profile = get_goal(user.goal)
    current = int(user.target_calories or 0)
    per_week = float(trend.get("per_week_kg") or 0.0)
    samples = int(trend.get("samples") or 0)
    lo, hi = profile.expected_rate_kg_week

    def build(delta: int, reason_ar: str, reason: str, direction: str) -> Recalibration:
        new_value, notes = clamp_calories(current + delta, user.gender)
        recal = Recalibration(
            changed=new_value != current,
            old_calories=current,
            new_calories=new_value,
            delta=new_value - current,
            reason=reason,
            reason_ar=reason_ar + (f" ({'؛ '.join(notes)})" if notes else ""),
            direction=direction,
            per_week_kg=per_week,
        )
        return recal

    if samples < 2:
        return build(0, "لا توجد قياسات كافية بعد لتعديل الخطة.", "not enough weigh-ins", "hold")

    if trend.get("stalled") or (lo <= per_week <= hi and abs(per_week) < 0.15):
        if profile.stall_direction == "decrease":
            return build(
                -200,
                f"وزنك ثابت منذ {weeks} أسابيع ({per_week:+.2f} كغ/أسبوع) → خفضت السعرات 200 لكسر الثبات.",
                f"weight flat for {weeks} weeks on a cut → −200 kcal",
                "decrease",
            )
        if profile.stall_direction == "increase":
            return build(
                220,
                f"وزنك ثابت منذ {weeks} أسابيع ({per_week:+.2f} كغ/أسبوع) → رفعت السعرات 220 لدعم البناء.",
                f"weight flat for {weeks} weeks on a bulk → +220 kcal",
                "increase",
            )
        return build(
            0,
            "الوزن ثابت وهذا مقبول لهدفك الحالي (إعادة تركيب/لياقة) — سنقيس بالمحيطات والصور لا بالميزان.",
            "flat weight is acceptable for this goal",
            "hold",
        )

    # moving too fast in either direction → protect muscle / limit fat gain
    if per_week < lo:
        if profile.key in (Goal.CUT, Goal.ENDURANCE):
            return build(
                180,
                f"نزولك أسرع من الآمن ({per_week:+.2f} كغ/أسبوع) → رفعت السعرات 180 لحماية العضل.",
                f"losing faster than safe ({per_week:+.2f} kg/wk) → +180 kcal",
                "increase",
            )
        return build(0, "الوزن ينزل — خارج نطاق هدفك لكن دون خطر.", "losing weight", "hold")
    if per_week > hi:
        if profile.key in (Goal.BULK, Goal.WEIGHT_GAIN, Goal.STRENGTH):
            return build(
                -180,
                f"زيادتك أسرع من المطلوب ({per_week:+.2f} كغ/أسبوع) → خفضت 180 سعرة لتقليل الدهون المصاحبة.",
                f"gaining faster than target ({per_week:+.2f} kg/wk) → −180 kcal",
                "decrease",
            )
        return build(0, "الوزن يزيد — خارج نطاق هدفك.", "gaining weight", "hold")

    return build(0, "التقدم ضمن النطاق المطلوب — لا تعديل.", "on track", "hold")


def expected_weight(start_kg: float, target_kg: float, days: int, rate_per_week: float = 0.5) -> float:
    """Where the user *should* be after ``days`` at a safe rate."""
    direction = -1 if target_kg < start_kg else 1
    return round(start_kg + direction * rate_per_week * (days / 7.0), 2)


def days_to_goal(current_kg: float, target_kg: float, rate_per_week: float = 0.5) -> int | None:
    if not current_kg or not target_kg or rate_per_week <= 0:
        return None
    delta = abs(current_kg - target_kg)
    return int(round(delta / rate_per_week * 7))


def protein_needs(weight_kg: float, goal: str | None, height_cm: float | None = None) -> int:
    profile = get_goal(goal)
    ref = adjusted_body_weight(weight_kg, height_cm)
    return max(settings.min_protein_g, int(round(ref * profile.protein_g_per_kg)))


def redistribute_remaining(
    *,
    remaining_calories: int,
    remaining_protein_g: float,
    remaining_carbs_g: float,
    remaining_fats_g: float,
    meals_left: int,
) -> list[dict[str, Any]]:
    """Split what is left of the day over the remaining meals (used when the user
    reports a skipped meal — spec §3.ب)."""
    meals_left = max(1, meals_left)
    per_meal = {
        "calories": int(round(remaining_calories / meals_left)),
        "protein_g": round(remaining_protein_g / meals_left, 1),
        "carbs_g": round(remaining_carbs_g / meals_left, 1),
        "fats_g": round(remaining_fats_g / meals_left, 1),
    }
    plan: list[dict[str, Any]] = []
    for index in range(meals_left):
        # front-load protein so the daily target is realistically reachable
        weight = 1.25 if index == 0 else (0.9 if index == meals_left - 1 else 1.0)
        plan.append(
            {
                "meal_index": index + 1,
                "calories": int(round(per_meal["calories"] * weight)),
                "protein_g": round(per_meal["protein_g"] * weight, 1),
                "carbs_g": round(per_meal["carbs_g"] * weight, 1),
                "fats_g": round(per_meal["fats_g"] * weight, 1),
            }
        )
    return plan


def format_targets_ar(user: User, targets: NutritionTargets | None = None) -> str:
    """Human-readable plan summary (Telegram HTML)."""
    targets = targets or compute_targets(user)
    goal_label = get_goal(targets.goal).label_ar
    value = bmi_label(targets.bmi, "ar")
    lines = [
        "<b>📊 حسابك العلمي</b>",
        f"• الهدف: {goal_label}",
        f"• BMR (أيضك الأساسي): {round(targets.bmr)} سعرة",
        f"• TDEE (حاجتك اليومية): {round(targets.tdee)} سعرة",
        f"• <b>سعراتك المستهدفة: {targets.calories}</b>",
        f"• البروتين: {targets.protein_g} جم | الكارب: {targets.carbs_g} جم | الدهون: {targets.fats_g} جم",
        f"• الماء: {targets.water_ml} مل يوميًا",
    ]
    if targets.bmi:
        lines.append(f"• مؤشر كتلة الجسم: {targets.bmi} ({value})")
    for note in targets.notes:
        lines.append(f"• ⚠️ {note}")
    return "\n".join(lines)
