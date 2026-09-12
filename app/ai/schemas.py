"""JSON schemas for structured AI outputs (OpenAI-compatible ``json_schema``).

Every schema is built with ``additionalProperties: false`` and *all* properties
listed in ``required`` — the strict-mode contract NanoGPT expects.  Optional
values are expressed as nullable types instead of being omitted.
"""
from __future__ import annotations

from typing import Any


def _obj(properties: dict[str, Any], *, required: list[str] | None = None) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required if required is not None else list(properties.keys()),
        "additionalProperties": False,
    }


def _arr(items: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {"type": "array", "items": items, **extra}


def _str(**extra: Any) -> dict[str, Any]:
    return {"type": "string", **extra}


def _num(**extra: Any) -> dict[str, Any]:
    return {"type": "number", **extra}


def _int(**extra: Any) -> dict[str, Any]:
    return {"type": "integer", **extra}


def _bool(**extra: Any) -> dict[str, Any]:
    return {"type": "boolean", **extra}


def _nullable(schema: dict[str, Any]) -> dict[str, Any]:
    return {"anyOf": [schema, {"type": "null"}]}


# ═══════════════════════════════════════════════════════════════════════════
#  FOOD ANALYSIS (vision)
# ═══════════════════════════════════════════════════════════════════════════
FOOD_ITEM_SCHEMA = _obj(
    {
        "name": _str(description="اسم الصنف كما يظهر في الطبق"),
        "portion_g": _num(description="الكمية التقديرية بالجرام"),
        "cooking_method": _str(description="مقلي/مشوي/مسلوق/نيء/غير معروف"),
        "calories": _int(),
        "protein_g": _num(),
        "carbs_g": _num(),
        "fats_g": _num(),
        "confidence": _num(description="0..1"),
    }
)

FOOD_ANALYSIS_SCHEMA = {
    "name": "food_analysis",
    "strict": True,
    "schema": _obj(
        {
            "meal_name": _str(description="اسم الوجبة المقترح"),
            "items": _arr(FOOD_ITEM_SCHEMA),
            "totals": _obj(
                {
                    "calories": _int(),
                    "protein_g": _num(),
                    "carbs_g": _num(),
                    "fats_g": _num(),
                }
            ),
            "assumptions": _arr(_str(), description="افتراضات التقدير (نوع الزيت، حجم الحصة…)"),
            "clarifications": _arr(
                _str(), description="أسئلة محددة جدًا عند الغموض — فارغة إن لم يلزم"
            ),
            "confidence": _num(description="0..1 للتحليل ككل"),
            "is_food": _bool(description="false إذا لم تكن الصورة طعامًا"),
            "notes": _nullable(_str()),
        }
    ),
}

# ═══════════════════════════════════════════════════════════════════════════
#  PROGRESS PHOTO (vision)
# ═══════════════════════════════════════════════════════════════════════════
PROGRESS_PHOTO_SCHEMA = {
    "name": "progress_photo_analysis",
    "strict": True,
    "schema": _obj(
        {
            "is_body_photo": _bool(),
            "estimated_body_fat_pct_range": _nullable(_str(description="مثل: 18-22%")),
            "muscle_development": _nullable(_str()),
            "posture_issues": _arr(_str()),
            "symmetry_notes": _nullable(_str()),
            "visible_changes": _arr(_str(), description="مقارنة بالصورة الأقدم إن وُجدت"),
            "weight_estimate_kg_range": _nullable(_str()),
            "injury_or_pain_signs": _arr(_str()),
            "photo_quality_notes": _nullable(_str(description="إضاءة/زاوية/ملابس تؤثر على الدقة")),
            "recommendations": _arr(_str()),
            "confidence": _num(),
        }
    ),
}

# ═══════════════════════════════════════════════════════════════════════════
#  TRAINING PLAN
# ═══════════════════════════════════════════════════════════════════════════
EXERCISE_SCHEMA = _obj(
    {
        "name": _str(),
        "target_muscle": _str(),
        "sets": _int(),
        "reps": _nullable(_str(description="مثل: 8-12 أو '45 ثانية'")),
        "rest_seconds": _int(),
        "weight_hint": _nullable(_str(description="توجيه للحمل: RPE/نسبة من 1RM/وصف")),
        "cue": _nullable(_str(description="ملاحظة تنفيذ/تصحيح")),
        "video_url": _nullable(_str()),
        "alternative_for": _nullable(_str(description="بديل عن تمرين ممنوع بسبب إصابة")),
    }
)

DAY_SCHEMA = _obj(
    {
        "day_index": _int(),
        "day_name": _str(),
        "focus": _str(),
        "is_rest": _bool(),
        "estimated_minutes": _int(),
        "estimated_calories_burned": _int(),
        "warmup": _arr(_str()),
        "exercises": _arr(EXERCISE_SCHEMA),
        "cooldown": _arr(_str()),
        "notes": _nullable(_str()),
    }
)

WEEK_SCHEMA = _obj(
    {
        "week_index": _int(),
        "title": _str(),
        "focus": _str(),
        "progression": _str(description="كيف يزيد الحمل عن الأسبوع السابق"),
        "days": _arr(DAY_SCHEMA),
    }
)

WORKOUT_PLAN_SCHEMA = {
    "name": "workout_plan",
    "strict": True,
    "schema": _obj(
        {
            "title": _str(),
            "goal": _str(),
            "equipment": _str(),
            "weeks_total": _int(),
            "weeks": _arr(WEEK_SCHEMA),
            "safety_notes": _arr(_str()),
            "restricted_exercises": _arr(_str(), description="تمارين مستبعدة بسبب إصابات/حالة صحية"),
            "progression_rules": _arr(_str()),
            "metrics_to_track": _arr(_str()),
        }
    ),
}

# ═══════════════════════════════════════════════════════════════════════════
#  NUTRITION PLAN (budget-aware, local food)
# ═══════════════════════════════════════════════════════════════════════════
MEAL_SCHEMA = _obj(
    {
        "slot": _str(description="breakfast|lunch|dinner|snack"),
        "name": _str(),
        "ingredients": _arr(_obj({"name": _str(), "grams": _num()})),
        "calories": _int(),
        "protein_g": _num(),
        "carbs_g": _num(),
        "fats_g": _num(),
        "cost_tier": _str(description="cheap|medium|expensive"),
        "method": _str(description="طريقة تحضير من سطرين"),
        "swaps": _arr(_str(), description="بدائل أرخص/متوفرة"),
    }
)

NUTRITION_PLAN_SCHEMA = {
    "name": "nutrition_plan",
    "strict": True,
    "schema": _obj(
        {
            "title": _str(),
            "daily_calories": _int(),
            "daily_protein_g": _int(),
            "daily_carbs_g": _int(),
            "daily_fats_g": _int(),
            "meals_per_day": _int(),
            "meals": _arr(MEAL_SCHEMA),
            "shopping_list": _arr(_obj({"item": _str(), "weekly_amount": _str(), "cost_tier": _str()})),
            "budget_notes": _arr(_str()),
            "hydration_ml": _int(),
            "supplement_notes": _arr(_str()),
        }
    ),
}

# ═══════════════════════════════════════════════════════════════════════════
#  BOXING PROGRAMME (phased — spec §5.13)
# ═══════════════════════════════════════════════════════════════════════════
BOXING_SCHEMA = {
    "name": "boxing_programme",
    "strict": True,
    "schema": _obj(
        {
            "title": _str(),
            "phase": _int(description="رقم الأسبوع/المرحلة"),
            "phase_goal": _str(),
            "skills": _arr(
                _obj(
                    {
                        "name": _str(),
                        "why": _str(),
                        "how": _str(),
                        "common_mistakes": _arr(_str()),
                        "video_url": _nullable(_str()),
                    }
                )
            ),
            "days": _arr(
                _obj(
                    {
                        "day_index": _int(),
                        "focus": _str(),
                        "is_rest": _bool(),
                        "warmup": _arr(_str()),
                        "skill_block": _arr(_str()),
                        "conditioning_block": _arr(_str()),
                        "punch_strength_block": _arr(_str(), description="تمارين تقوية اللكمة والرسغ"),
                        "cooldown": _arr(_str()),
                        "rounds": _nullable(_str(description="مثل: 6 جولات × 2 دقيقة عمل / 1 دقيقة راحة")),
                        "estimated_minutes": _int(),
                    }
                )
            ),
            "next_phase_preview": _str(),
            "safety_notes": _arr(_str()),
            "gear_needed": _arr(_str(description="يمكن أن تكون فارغة: تدريب بدون معدات"),),
        }
    ),
}

# ═══════════════════════════════════════════════════════════════════════════
#  SAFETY SCREEN
# ═══════════════════════════════════════════════════════════════════════════
SAFETY_SCHEMA = {
    "name": "safety_screen",
    "strict": True,
    "schema": _obj(
        {
            "red_flag": _bool(),
            "conditions": _arr(_str()),
            "restricted_movements": _arr(_str()),
            "safe_alternatives": _arr(_str()),
            "medical_referral": _str(),
            "notes": _str(),
        }
    ),
}

# ═══════════════════════════════════════════════════════════════════════════
#  DAY RECALCULATION (skipped meal / missed protein)
# ═══════════════════════════════════════════════════════════════════════════
DAY_RECALC_SCHEMA = {
    "name": "day_recalculation",
    "strict": True,
    "schema": _obj(
        {
            "explanation": _str(description="ماذا تغيّر ولماذا — سطرين"),
            "new_remaining_calories": _int(),
            "new_remaining_protein_g": _num(),
            "new_remaining_carbs_g": _num(),
            "new_remaining_fats_g": _num(),
            "suggested_meals": _arr(MEAL_SCHEMA),
            "warnings": _arr(_str()),
        }
    ),
}

# ═══════════════════════════════════════════════════════════════════════════
#  CONSULTATION (structured so every exercise gets a real video link)
# ═══════════════════════════════════════════════════════════════════════════
CONSULT_SCHEMA = {
    "name": "consultation_answer",
    "strict": True,
    "schema": _obj(
        {
            "answer": _str(description="الجواب الكامل بتنسيق تيلجرام (<b> و •) بدون markdown"),
            "exercises": _arr(
                _obj(
                    {
                        "name": _str(),
                        "how_to": _str(description="شرح تنفيذ مختصر"),
                        "sets_reps": _nullable(_str(description="مثل: 3×12 أو 40 ثانية")),
                        "target_muscle": _nullable(_str()),
                        "calories_estimate": _nullable(_int()),
                        "video_query": _str(description="كلمات بحث دقيقة للفيديو (عربي/إنجليزي)"),
                        "contraindicated_for": _arr(_str(), description="إصابات تمنع هذا التمرين"),
                    }
                ),
                description="فارغة إذا لم يطلب المستخدم تمارين",
            ),
            "meals": _arr(
                _obj(
                    {
                        "name": _str(),
                        "calories": _int(),
                        "protein_g": _num(),
                        "carbs_g": _num(),
                        "fats_g": _num(),
                        "note": _nullable(_str()),
                    }
                ),
                description="فارغة إذا لم يطلب وجبات",
            ),
            "follow_up_question": _nullable(_str(description="سؤال ذكي واحد إن كانت معلومة ناقصة")),
            "safety_note": _nullable(_str()),
            "estimated_total_calories": _nullable(_int(description="مثل: إجمالي حرق التمارين المقترحة")),
        }
    ),
}

SCHEMAS: dict[str, dict[str, Any]] = {
    "consult": CONSULT_SCHEMA,
    "food": FOOD_ANALYSIS_SCHEMA,
    "photo": PROGRESS_PHOTO_SCHEMA,
    "workout": WORKOUT_PLAN_SCHEMA,
    "nutrition": NUTRITION_PLAN_SCHEMA,
    "boxing": BOXING_SCHEMA,
    "safety": SAFETY_SCHEMA,
    "recalc": DAY_RECALC_SCHEMA,
}
