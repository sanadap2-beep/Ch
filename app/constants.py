"""Project-wide enumerations and constant registries.

Everything that a future feature may need to extend (goals, equipment,
activity levels, badge keys, AI tasks…) lives here as a *string* enum so it
can be persisted directly in PostgreSQL without custom types and can be
compared safely in Python.
"""
from __future__ import annotations

from enum import StrEnum


class Language(StrEnum):
    AR = "ar"
    EN = "en"


class Gender(StrEnum):
    MALE = "male"
    FEMALE = "female"


class ActivityLevel(StrEnum):
    """Daily activity level — the multiplier applied to BMR to get TDEE."""

    SEDENTARY = "sedentary"        # خامل (مكتبى، قليل الحركة)
    LIGHT = "light"                # نشيط قليلًا (تمارين 1-3 أيام)
    MODERATE = "moderate"          # نشيط (تمارين 3-5 أيام)
    VERY_ACTIVE = "very_active"    # نشيط جدًا (تمارين 6-7 أيام)
    ATHLETE = "athlete"            # رياضي محترف (تمرينين باليوم / عمل بدني)


ACTIVITY_MULTIPLIERS: dict[ActivityLevel, float] = {
    ActivityLevel.SEDENTARY: 1.2,
    ActivityLevel.LIGHT: 1.375,
    ActivityLevel.MODERATE: 1.55,
    ActivityLevel.VERY_ACTIVE: 1.725,
    ActivityLevel.ATHLETE: 1.9,
}


class Equipment(StrEnum):
    GYM = "gym"                          # نادي رياضي كامل
    HOME_BASIC = "home_basic"            # بيت + معدات بسيطة (دمبل/حبل/بار)
    HOME_NO_EQUIPMENT = "home_no_equipment"  # بيت بدون أي معدات
    CALISTHENICS_PARK = "calisthenics_park"  # بارك/عقل


class Goal(StrEnum):
    """Training goal.

    The goal is *not* hard-coded in the coaching logic: every goal is backed by
    a :class:`~app.services.goal_registry.GoalProfile` that carries its own
    calorie adjustment, macro split, plan hints and prompt flavour.  Adding a
    new coach/goal later = adding one enum member + one profile, no refactor.
    """

    CUT = "cut"                    # تنشيف / فقدان دهون
    BULK = "bulk"                  # تضخيم / بناء عضل
    RECOMP = "recomp"              # تنشيط عام (إعادة تركيب الجسم)
    GENERAL_FITNESS = "general_fitness"   # تحسين لياقة
    STRENGTH = "strength"          # زيادة قوة
    ENDURANCE = "endurance"        # تحمل ولياقة قلبية
    BOXING = "boxing"              # تعلم ملاكمة من الصفر
    FLEXIBILITY = "flexibility"    # مرونة واستطالة
    HEIGHT = "height"              # زيادة الطول (صغار السن)
    WEIGHT_GAIN = "weight_gain"    # زيادة وزن (نحافة مفرطة)
    GENERAL_HEALTH = "general_health"  # صحة عامة
    CUSTOM = "custom"              # هدف مخصص يصفه المستخدم نصيًا


class BudgetLevel(StrEnum):
    LOW = "low"          # ميزانية محدودة جدًا
    MEDIUM = "medium"
    HIGH = "high"


class FoodStyle(StrEnum):
    """Cuisine / food-style presets.  Free-text is also accepted and stored
    in ``users.food_style_note``."""

    LOCAL_POPULAR = "local_popular"   # أكل شعبي محلي
    HOME_COOKED = "home_cooked"       # أكل بيتي
    MEDITERRANEAN = "mediterranean"
    VEGETARIAN = "vegetarian"
    VEGAN = "vegan"
    HALAL_STRICT = "halal_strict"
    FAST_FOOD_OK = "fast_food_ok"     # لا مانع من الوجبات السريعة أحيانًا
    ANY = "any"


class PhotoAngle(StrEnum):
    FRONT = "front"
    SIDE = "side"
    BACK = "back"
    ARMS = "arms"
    ABS = "abs"
    OTHER = "other"


class MeasurementType(StrEnum):
    WAIST = "waist"
    CHEST = "chest"
    ARM = "arm"
    THIGH = "thigh"
    HIP = "hip"
    NECK = "neck"
    CALF = "calf"
    SHOULDER = "shoulder"


class PointsReason(StrEnum):
    ADMIN_GRANT = "admin_grant"
    ADMIN_DEDUCT = "admin_deduct"
    WELCOME_BONUS = "welcome_bonus"
    REFERRAL_INVITER = "referral_inviter"
    REFERRAL_INVITEE = "referral_invitee"
    STREAK_REWARD = "streak_reward"
    SUBSCRIPTION_RENEW = "subscription_renew"
    COACH_ACCESS = "coach_access"
    COACH_MESSAGE = "coach_message"
    VISION_FOOD = "vision_food"
    VISION_PROGRESS = "vision_progress"
    REPORT_EXPORT = "report_export"
    REFUND = "refund"
    ADJUSTMENT = "adjustment"


POINTS_SPEND_REASONS = frozenset(
    {
        PointsReason.COACH_ACCESS,
        PointsReason.COACH_MESSAGE,
        PointsReason.VISION_FOOD,
        PointsReason.VISION_PROGRESS,
        PointsReason.REPORT_EXPORT,
        PointsReason.ADMIN_DEDUCT,
    }
)


class SubscriptionPlan(StrEnum):
    NONE = "none"
    MONTHLY = "monthly"      # تجديد نقاط تلقائي كل 30 يوم
    QUARTERLY = "quarterly"  # كل 90 يوم


class AITask(StrEnum):
    """Logical task → model routing key (see :mod:`app.ai.router`)."""

    CONSULT = "consult"                 # استشارة عامة (مجانية، حجم عالي)
    COACH = "coach"                       # كوتش شخصي نصي
    VISION_FOOD = "vision_food"           # تحليل صورة أكل
    VISION_PROGRESS = "vision_progress"   # تحليل/مقارنة صور الجسم
    PLAN = "plan"                         # توليد برنامج تمارين/وجبات (JSON)
    REPORT = "report"                     # تقارير أسبوعية/شهرية
    SUMMARY = "summary"                   # تلخيص سجل المحادثة
    STT = "stt"                           # تحويل صوت لنص
    SAFETY = "safety"                     # فحص الموانع الصحية
    BOXING = "boxing"                     # برنامج ملاكمة مرحلي


class MealKind(StrEnum):
    PHOTO = "photo"              # من صورة
    TEXT = "text"                # وصف نصي حلّله الكوتش
    MANUAL = "manual"            # إدخال يدوي للسعرات
    SKIPPED = "skipped"          # وجبة لم تُؤكل → إعادة حساب اليوم
    ADJUSTMENT = "adjustment"    # تصحيح/تعديل من الكوتش
    SUPPLEMENT = "supplement"    # مكمل (بروتين بودر مثلًا)


class ReminderKind(StrEnum):
    WATER = "water"
    WORKOUT = "workout"
    WEIGH_IN = "weigh_in"
    MEAL_LOG = "meal_log"
    PROGRESS_PHOTO = "progress_photo"
    COACH_CHECKIN = "coach_checkin"


class BadgeKey(StrEnum):
    PROFILE_COMPLETE = "profile_complete"
    STREAK_3 = "streak_3"
    STREAK_7 = "streak_7"
    STREAK_30 = "streak_30"
    STREAK_100 = "streak_100"
    FIRST_WORKOUT = "first_workout"
    FIRST_MEAL_LOGGED = "first_meal_logged"
    WEIGHT_LOSS_5KG = "weight_loss_5kg"
    WEIGHT_LOSS_10KG = "weight_loss_10kg"
    WEIGHT_GAIN_5KG = "weight_gain_5kg"
    PHOTO_MONTH = "photo_month"
    REFERRED_5 = "referred_5"
    BOXING_PHASE_1 = "boxing_phase_1"


class InjuryArea(StrEnum):
    NONE = "none"
    KNEE = "knee"
    LOWER_BACK = "lower_back"
    SHOULDER = "shoulder"
    NECK = "neck"
    ANKLE = "ankle"
    WRIST = "wrist"
    ELBOW = "elbow"
    HIP = "hip"
    CHEST_HEART = "chest_heart"
    OTHER = "other"


# Conditions that *must* trigger a medical-referral warning before the coach
# prescribes anything intense (spec §5.6 — legal & ethical protection).
RED_FLAG_CONDITIONS: frozenset[str] = frozenset(
    {
        "heart_disease",
        "high_blood_pressure_uncontrolled",
        "diabetes_type1",
        "recent_surgery",
        "pregnancy",
        "herniated_disc",
        "osteoporosis",
        "kidney_disease",
        "epilepsy",
        "severe_asthma",
    }
)


class BroadcastStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


class PlanStatus(StrEnum):
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    COMPLETED = "completed"


class AdminAction(StrEnum):
    BAN = "ban"
    UNBAN = "unban"
    GRANT_POINTS = "grant_points"
    DEDUCT_POINTS = "deduct_points"
    VIEW_PROFILE = "view_profile"
    BROADCAST = "broadcast"
    SET_SUBSCRIPTION = "set_subscription"
    RESET_PROGRESS = "reset_progress"
    DELETE_MEDIA = "delete_media"
    CONFIG_CHANGE = "config_change"


# ── misc tunables ────────────────────────────────────────────────────────────
TELEGRAM_MESSAGE_LIMIT = 4096          # hard Telegram cap
TELEGRAM_CHUNK_LIMIT = 3900            # safe chunk size after HTML escaping
MAX_AI_REPLY_CHARS = 3800
WATER_ML_PER_KG = 33                   # baseline hydration target
STALL_WEEKS_BEFORE_ADJUST = 2          # وزن ثابت أسبوعين → تعديل السعرات
COACH_HISTORY_TURNS = 24               # raw turns kept in context
COACH_SUMMARY_TRIGGER = 40             # turns before summarisation kicks in
