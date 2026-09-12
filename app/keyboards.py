"""All keyboards (reply + inline) for the bot, Arabic/English aware."""
from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder

from app.callbacks import (
    AdminCB,
    AdminUserCB,
    CoachCB,
    ConsultCB,
    ForcedCB,
    MenuCB,
    OnboardingCB,
    PlanCB,
    PointsCB,
    PrivacyCB,
    ProgressCB,
    ReferralCB,
    ReminderCB,
    ScanCB,
    SettingsCB,
)
from app.config import settings
from app.constants import (
    ActivityLevel,
    BudgetLevel,
    Equipment,
    FoodStyle,
    Gender,
    Goal,
    ReminderKind,
    SubscriptionPlan,
)
from app.services.goal_registry import all_goals

# ═══════════════════════════════════════════════════════════════════════════
#  label tables
# ═══════════════════════════════════════════════════════════════════════════
GENDER_LABELS = {
    Gender.MALE.value: ("👨 ذكر", "👨 Male"),
    Gender.FEMALE.value: ("👩 أنثى", "👩 Female"),
}
ACTIVITY_LABELS = {
    ActivityLevel.SEDENTARY.value: ("🪑 خامل (مكتبي)", "🪑 Sedentary (desk job)"),
    ActivityLevel.LIGHT.value: ("🚶 نشيط قليلًا (1-3 أيام)", "🚶 Lightly active (1-3 days)"),
    ActivityLevel.MODERATE.value: ("🏃 نشيط (3-5 أيام)", "🏃 Moderately active (3-5 days)"),
    ActivityLevel.VERY_ACTIVE.value: ("🏋️ نشيط جدًا (6-7 أيام)", "🏋️ Very active (6-7 days)"),
    ActivityLevel.ATHLETE.value: ("🥇 رياضي محترف", "🥇 Professional athlete"),
}
EQUIPMENT_LABELS = {
    Equipment.GYM.value: ("🏢 نادي رياضي كامل", "🏢 Full gym"),
    Equipment.HOME_BASIC.value: ("🏠 بيت + معدات بسيطة", "🏠 Home + basic equipment"),
    Equipment.HOME_NO_EQUIPMENT.value: ("🛋️ بيت بدون معدات", "🛋️ Home, no equipment"),
    Equipment.CALISTHENICS_PARK.value: ("🌳 بارك/عقل", "🌳 Calisthenics park"),
}
BUDGET_LABELS = {
    BudgetLevel.LOW.value: ("💸 محدودة — أرخص الخيارات", "💸 Tight — cheapest options"),
    BudgetLevel.MEDIUM.value: ("💰 متوسطة", "💰 Medium"),
    BudgetLevel.HIGH.value: ("💎 مفتوحة", "💎 Open budget"),
}
FOOD_LABELS = {
    FoodStyle.LOCAL_POPULAR.value: ("🥘 أكل شعبي محلي", "🥘 Local popular food"),
    FoodStyle.HOME_COOKED.value: ("🍲 أكل بيتي", "🍲 Home-cooked"),
    FoodStyle.MEDITERRANEAN.value: ("🫒 متوسطي", "🫒 Mediterranean"),
    FoodStyle.VEGETARIAN.value: ("🥗 نباتي", "🥗 Vegetarian"),
    FoodStyle.VEGAN.value: ("🌱 نباتي صرف", "🌱 Vegan"),
    FoodStyle.HALAL_STRICT.value: ("🕌 حلال فقط", "🕌 Halal only"),
    FoodStyle.FAST_FOOD_OK.value: ("🍔 لا مانع من الوجبات السريعة", "🍔 Fast food OK sometimes"),
    FoodStyle.ANY.value: ("🍽️ أي نوع", "🍽️ Anything"),
}
ANGLE_LABELS = {
    "front": ("🧍 أمام", "🧍 Front"),
    "side": ("↔️ جنب", "↔️ Side"),
    "back": ("🔙 خلف", "🔙 Back"),
    "arms": ("💪 الذراعين", "💪 Arms"),
    "abs": ("🔥 البطن", "🔥 Abs"),
    "other": ("📷 أخرى", "📷 Other"),
}
REMINDER_LABELS = {
    ReminderKind.WATER.value: ("💧 الماء", "💧 Water"),
    ReminderKind.WORKOUT.value: ("🏋️ التمرين", "🏋️ Workout"),
    ReminderKind.WEIGH_IN.value: ("⚖️ الميزان الأسبوعي", "⚖️ Weekly weigh-in"),
    ReminderKind.MEAL_LOG.value: ("🍽️ تسجيل الوجبات", "🍽️ Meal logging"),
    ReminderKind.PROGRESS_PHOTO.value: ("📷 صورة التقدم", "📷 Progress photo"),
    ReminderKind.COACH_CHECKIN.value: ("🤝 متابعة الكوتش", "🤝 Coach check-in"),
}


def _label(table: dict[str, tuple[str, str]], key: str, lang: str) -> str:
    entry = table.get(key)
    if not entry:
        return key
    return entry[0] if str(lang or "ar").startswith("ar") else entry[1]


def is_ar(lang: str | None) -> bool:
    return not str(lang or "ar").startswith("en")


# ═══════════════════════════════════════════════════════════════════════════
#  reply keyboards
# ═══════════════════════════════════════════════════════════════════════════
def remove_keyboard() -> ReplyKeyboardRemove:
    return ReplyKeyboardRemove()


def main_menu(lang: str = "ar", *, is_admin: bool = False, has_coach: bool = False) -> ReplyKeyboardMarkup:
    ar = is_ar(lang)
    builder = ReplyKeyboardBuilder()
    builder.row(
        KeyboardButton(text=("💬 الاستشارة العامة" if ar else "💬 General consult")),
        KeyboardButton(text=("🏆 الكوتش الشخصي 24س" if ar else "🏆 24h Coach")),
    )
    builder.row(
        KeyboardButton(text=("🍽️ حساب سعرات الأكل" if ar else "🍽️ Food calorie scan")),
        KeyboardButton(text=("📈 تقدمي" if ar else "📈 My progress")),
    )
    builder.row(
        KeyboardButton(text=("📋 برنامجي" if ar else "📋 My programme")),
        KeyboardButton(text=("🪙 نقاطي" if ar else "🪙 My points")),
    )
    builder.row(
        KeyboardButton(text=("🎁 دعوة صديق" if ar else "🎁 Invite a friend")),
        KeyboardButton(text=("⚙️ الإعدادات" if ar else "⚙️ Settings")),
    )
    if is_admin:
        builder.row(KeyboardButton(text=("🛡️ لوحة الأدمن" if ar else "🛡️ Admin panel")))
    return builder.as_markup(resize_keyboard=True, input_field_placeholder=(
        "اكتب سؤالك أو اختر من الأزرار…" if ar else "Type your question or pick a button…"
    ))


# ═══════════════════════════════════════════════════════════════════════════
#  inline: navigation helpers
# ═══════════════════════════════════════════════════════════════════════════
def home_button(lang: str = "ar") -> InlineKeyboardButton:
    return InlineKeyboardButton(
        text=("🏠 القائمة الرئيسية" if is_ar(lang) else "🏠 Main menu"),
        callback_data=MenuCB(action="home").pack(),
    )


def back_button(lang: str = "ar", action: str = "home") -> InlineKeyboardButton:
    return InlineKeyboardButton(
        text=("↩️ رجوع" if is_ar(lang) else "↩️ Back"),
        callback_data=MenuCB(action=action).pack(),
    )


def _kb(rows: list[list[InlineKeyboardButton]], lang: str = "ar", *, home: bool = True) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for row in rows:
        builder.row(*row)
    if home:
        builder.row(home_button(lang))
    return builder.as_markup()


# ═══════════════════════════════════════════════════════════════════════════
#  onboarding
# ═══════════════════════════════════════════════════════════════════════════
def onb_gender(lang: str) -> InlineKeyboardMarkup:
    return _kb(
        [[InlineKeyboardButton(text=_label(GENDER_LABELS, g.value, lang),
                               callback_data=OnboardingCB(step="gender", value=g.value).pack())
          for g in Gender]],
        lang,
    )


def onb_activity(lang: str) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=_label(ACTIVITY_LABELS, key, lang),
                              callback_data=OnboardingCB(step="activity", value=key).pack())]
        for key in ACTIVITY_LABELS
    ]
    return _kb(rows, lang)


def onb_goals(lang: str) -> InlineKeyboardMarkup:
    goals = all_goals(lang)
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for goal in goals:
        if goal["key"] == Goal.CUSTOM.value:
            continue
        row.append(InlineKeyboardButton(
            text=goal["label"], callback_data=OnboardingCB(step="goal", value=goal["key"]).pack()
        ))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton(
        text=("✍️ هدف مخصص (اكتبه)" if is_ar(lang) else "✍️ Custom goal (type it)"),
        callback_data=OnboardingCB(step="goal", value=Goal.CUSTOM.value).pack(),
    )])
    return _kb(rows, lang, home=False)


def onb_equipment(lang: str) -> InlineKeyboardMarkup:
    return _kb(
        [[InlineKeyboardButton(text=_label(EQUIPMENT_LABELS, key, lang),
                               callback_data=OnboardingCB(step="equipment", value=key).pack())]
         for key in EQUIPMENT_LABELS],
        lang,
    )


def onb_budget(lang: str) -> InlineKeyboardMarkup:
    return _kb(
        [[InlineKeyboardButton(text=_label(BUDGET_LABELS, key, lang),
                               callback_data=OnboardingCB(step="budget", value=key).pack())
          for key in BudgetLevel]],
        lang,
    )


def onb_food_style(lang: str) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for key in FOOD_LABELS:
        row.append(InlineKeyboardButton(text=_label(FOOD_LABELS, key, lang),
                                        callback_data=OnboardingCB(step="food", value=key).pack()))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton(
        text=("✍️ أكتب نوع أكلي" if is_ar(lang) else "✍️ Type my food style"),
        callback_data=OnboardingCB(step="food", value="_custom").pack(),
    )])
    return _kb(rows, lang, home=False)


def onb_cancel(lang: str) -> InlineKeyboardMarkup:
    return _kb(
        [[InlineKeyboardButton(text=("❌ إلغاء التسجيل" if is_ar(lang) else "❌ Cancel sign-up"),
                               callback_data=OnboardingCB(step="cancel").pack())]],
        lang, home=False,
    )


# ═══════════════════════════════════════════════════════════════════════════
#  privacy / forced subscription
# ═══════════════════════════════════════════════════════════════════════════
def privacy_consent(lang: str) -> InlineKeyboardMarkup:
    ar = is_ar(lang)
    return _kb(
        [[
            InlineKeyboardButton(text=("✅ أوافق" if ar else "✅ I accept"),
                                 callback_data=PrivacyCB(action="accept").pack()),
            InlineKeyboardButton(text=("❌ لا أوافق" if ar else "❌ Decline"),
                                 callback_data=PrivacyCB(action="decline").pack()),
        ]],
        lang, home=False,
    )


def privacy_menu(lang: str) -> InlineKeyboardMarkup:
    ar = is_ar(lang)
    return _kb(
        [
            [InlineKeyboardButton(text=("📜 سياسة الخصوصية" if ar else "📜 Privacy policy"),
                                  callback_data=PrivacyCB(action="policy").pack())],
            [InlineKeyboardButton(text=("📤 تصدير بياناتي (PDF)" if ar else "📤 Export my data (PDF)"),
                                  callback_data=PrivacyCB(action="export").pack())],
            [InlineKeyboardButton(text=("🗑️ حذف كل صوري" if ar else "🗑️ Delete all my photos"),
                                  callback_data=PrivacyCB(action="delete_photos").pack())],
            [InlineKeyboardButton(text=("☠️ حذف حسابي نهائيًا" if ar else "☠️ Delete my account"),
                                  callback_data=PrivacyCB(action="delete_account").pack())],
        ],
        lang,
    )


def confirm_deletion(lang: str) -> InlineKeyboardMarkup:
    ar = is_ar(lang)
    return _kb(
        [[
            InlineKeyboardButton(text=("☠️ نعم، احذف كل شيء" if ar else "☠️ Yes, delete everything"),
                                 callback_data=PrivacyCB(action="confirm_delete").pack()),
            InlineKeyboardButton(text=("↩️ تراجع" if ar else "↩️ Cancel"),
                                 callback_data=SettingsCB(action="privacy").pack()),
        ]],
        lang, home=False,
    )


def forced_subscription(channels: Sequence[str], lang: str) -> InlineKeyboardMarkup:
    ar = is_ar(lang)
    rows: list[list[InlineKeyboardButton]] = []
    for channel in channels:
        handle = channel.strip()
        if handle.startswith("@"):
            rows.append([InlineKeyboardButton(text=f"📢 {handle}", url=f"https://t.me/{handle[1:]}")])
        elif handle.startswith("-100") or handle.lstrip("-").isdigit():
            rows.append([InlineKeyboardButton(text=f"📢 {handle}", url=f"https://t.me/c/{handle.replace('-100', '')}/1")])
        elif handle.startswith("http"):
            rows.append([InlineKeyboardButton(text=("📢 القناة" if ar else "📢 Channel"), url=handle)])
    rows.append([InlineKeyboardButton(text=("🔄 تحقّق من اشتراكي" if ar else "🔄 Verify my subscription"),
                                      callback_data=ForcedCB(action="check").pack())])
    return _kb(rows, lang, home=False)


# ═══════════════════════════════════════════════════════════════════════════
#  consult / coach / scan
# ═══════════════════════════════════════════════════════════════════════════
def consult_menu(lang: str, examples: Sequence[str] | None = None) -> InlineKeyboardMarkup:
    ar = is_ar(lang)
    rows: list[list[InlineKeyboardButton]] = []
    if examples:
        # Telegram caps callback_data at 64 bytes, so buttons carry the example's
        # index and the handler resolves the text — Arabic examples are ~2 bytes/char
        # and would blow the limit (and crash the whole panel) if packed directly.
        for index, example in enumerate(list(examples)[:4]):
            rows.append([InlineKeyboardButton(
                text=(example[:60] + ("…" if len(example) > 60 else "")),
                callback_data=ConsultCB(action="example", arg=str(index)).pack(),
            )])
    rows.append([InlineKeyboardButton(text=("✍️ سؤال جديد" if ar else "✍️ New question"),
                                      callback_data=ConsultCB(action="start").pack())])
    return _kb(rows, lang)


def consult_examples(lang: str) -> list[str]:
    if is_ar(lang):
        return [
            "أعطني تمارين تحرق 300 سعرة",
            "تمارين بيت بدون معدات للصدر والظهر",
            "كيف أبلش ملاكمة من البيت؟",
            "تمارين استطالة لزيادة الطول",
            "وش آكل قبل التمرين بساعة؟",
            "أرخص مصادر بروتين بالسوق",
        ]
    return [
        "Give me exercises that burn 300 kcal",
        "No-equipment home workout for chest & back",
        "How do I start boxing at home?",
        "Stretching routine to support height",
        "What should I eat an hour before training?",
        "Cheapest protein sources at the market",
    ]


def coach_menu(lang: str, *, unlocked: bool, balance: int, today_done: bool = False) -> InlineKeyboardMarkup:
    ar = is_ar(lang)
    rows: list[list[InlineKeyboardButton]] = []
    if not unlocked:
        rows.append([InlineKeyboardButton(
            text=(f"🔓 افتح الكوتش ({settings.coach_access_cost_points} نقطة)" if ar
                  else f"🔓 Unlock coach ({settings.coach_access_cost_points} points)"),
            callback_data=CoachCB(action="confirm_unlock").pack(),
        )])
    else:
        rows.append([InlineKeyboardButton(text=("📊 حالة يومي الآن" if ar else "📊 My day right now"),
                                          callback_data=CoachCB(action="status").pack())])
        rows.append([
            InlineKeyboardButton(text=("✅ خلصت تمرين اليوم" if ar else "✅ Workout done"),
                                 callback_data=CoachCB(action="workout_done").pack()),
            InlineKeyboardButton(text=("🚫 فوّت وجبة" if ar else "🚫 Skipped a meal"),
                                 callback_data=CoachCB(action="skip_meal").pack()),
        ])
        rows.append([
            InlineKeyboardButton(text=("🍽️ اقترح وجباتي اليوم" if ar else "🍽️ Suggest today's meals"),
                                 callback_data=CoachCB(action="meals").pack()),
            InlineKeyboardButton(text=("🗓️ تقريري الأسبوعي" if ar else "🗓️ Weekly report"),
                                 callback_data=CoachCB(action="report").pack()),
        ])
        rows.append([InlineKeyboardButton(text=(f"🪙 رصيدي: {balance}" if ar else f"🪙 Balance: {balance}"),
                                          callback_data=PointsCB(action="balance").pack())])
    return _kb(rows, lang)


def scan_menu(lang: str, *, free_left: int, balance: int) -> InlineKeyboardMarkup:
    ar = is_ar(lang)
    return _kb(
        [
            [InlineKeyboardButton(text=("📸 أرسل صورة الوجبة" if ar else "📸 Send a meal photo"),
                                  callback_data=ScanCB(action="start").pack())],
            [InlineKeyboardButton(text=("✍️ أدخل وجبة يدويًا" if ar else "✍️ Log a meal manually"),
                                  callback_data=ScanCB(action="manual").pack())],
            [InlineKeyboardButton(text=("📊 ملخص يومي" if ar else "📊 Today's summary"),
                                  callback_data=ScanCB(action="today").pack())],
            [InlineKeyboardButton(
                text=(f"🎁 مجاني متبقي: {free_left} | 🪙 {balance}" if ar else f"🎁 Free left: {free_left} | 🪙 {balance}"),
                callback_data=PointsCB(action="balance").pack())],
        ],
        lang,
    )


# ═══════════════════════════════════════════════════════════════════════════
#  progress / plans
# ═══════════════════════════════════════════════════════════════════════════
def progress_menu(lang: str) -> InlineKeyboardMarkup:
    ar = is_ar(lang)
    return _kb(
        [
            [InlineKeyboardButton(text=("⚖️ تسجيل الوزن" if ar else "⚖️ Log weight"),
                                  callback_data=ProgressCB(action="weight").pack()),
             InlineKeyboardButton(text=("📏 القياسات" if ar else "📏 Measurements"),
                                  callback_data=ProgressCB(action="measurements").pack())],
            [InlineKeyboardButton(text=("📷 صورة تقدم" if ar else "📷 Progress photo"),
                                  callback_data=ProgressCB(action="photo").pack()),
             InlineKeyboardButton(text=("📉 الرسم البياني" if ar else "📉 Chart"),
                                  callback_data=ProgressCB(action="chart").pack())],
            [InlineKeyboardButton(text=("🗓️ تقرير أسبوعي" if ar else "🗓️ Weekly report"),
                                  callback_data=ProgressCB(action="report_week").pack()),
             InlineKeyboardButton(text=("📆 تقرير شهري" if ar else "📆 Monthly report"),
                                  callback_data=ProgressCB(action="report_month").pack())],
            [InlineKeyboardButton(text=("📄 تصدير رحلتي PDF" if ar else "📄 Export journey (PDF)"),
                                  callback_data=ProgressCB(action="export").pack())],
        ],
        lang,
    )


def photo_angles(lang: str) -> InlineKeyboardMarkup:
    return _kb(
        [[InlineKeyboardButton(text=_label(ANGLE_LABELS, key, lang),
                               callback_data=ProgressCB(action="angle", arg=key).pack())
          for key in list(ANGLE_LABELS)[:3]],
         [InlineKeyboardButton(text=_label(ANGLE_LABELS, key, lang),
                               callback_data=ProgressCB(action="angle", arg=key).pack())
          for key in list(ANGLE_LABELS)[3:]]],
        lang,
    )


def plan_menu(lang: str, *, has_plan: bool, kind: str | None = None) -> InlineKeyboardMarkup:
    ar = is_ar(lang)
    rows: list[list[InlineKeyboardButton]] = []
    if has_plan:
        rows.append([InlineKeyboardButton(text=("📅 تمرين اليوم" if ar else "📅 Today's session"),
                                          callback_data=PlanCB(action="today").pack())])
        rows.append([InlineKeyboardButton(text=("✅ أنهيت تمرين اليوم" if ar else "✅ Mark today done"),
                                          callback_data=PlanCB(action="mark_done").pack())])
        if kind == "boxing":
            rows.append([InlineKeyboardButton(text=("🥊 المرحلة التالية" if ar else "🥊 Next phase"),
                                              callback_data=PlanCB(action="next_phase", kind="boxing").pack())])
    rows.append([InlineKeyboardButton(text=("🏋️ توليد برنامج تمارين" if ar else "🏋️ Generate training plan"),
                                      callback_data=PlanCB(action="generate", kind="training").pack())])
    rows.append([InlineKeyboardButton(text=("🍽️ توليد خطة وجبات" if ar else "🍽️ Generate meal plan"),
                                      callback_data=PlanCB(action="generate", kind="nutrition").pack())])
    rows.append([
        InlineKeyboardButton(text=("🥊 برنامج ملاكمة" if ar else "🥊 Boxing programme"),
                             callback_data=PlanCB(action="generate", kind="boxing").pack()),
        InlineKeyboardButton(text=("🧘 استطالة ومرونة" if ar else "🧘 Mobility"),
                             callback_data=PlanCB(action="generate", kind="flexibility").pack()),
    ])
    return _kb(rows, lang)


# ═══════════════════════════════════════════════════════════════════════════
#  points / referral / subscription
# ═══════════════════════════════════════════════════════════════════════════
def points_menu(lang: str, page: int = 0) -> InlineKeyboardMarkup:
    ar = is_ar(lang)
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(text=("📒 سجل النقاط" if ar else "📒 Points ledger"),
                              callback_data=PointsCB(action="ledger", page=page).pack())],
        [InlineKeyboardButton(text=("🎁 دعوة صديق = نقاط" if ar else "🎁 Invite a friend = points"),
                              callback_data=ReferralCB(action="share").pack()),
         InlineKeyboardButton(text=("📅 اشتراكي" if ar else "📅 My subscription"),
                              callback_data=PointsCB(action="subscription").pack())],
        [InlineKeyboardButton(text=("➕ كيف أحصل على نقاط؟" if ar else "➕ How do I get points?"),
                              callback_data=PointsCB(action="get").pack())],
    ]
    return _kb(rows, lang)


def ledger_pager(lang: str, page: int, has_more: bool) -> InlineKeyboardMarkup:
    ar = is_ar(lang)
    row: list[InlineKeyboardButton] = []
    if page > 0:
        row.append(InlineKeyboardButton(text=("⬅️ السابق" if ar else "⬅️ Prev"),
                                        callback_data=PointsCB(action="ledger", page=page - 1).pack()))
    if has_more:
        row.append(InlineKeyboardButton(text=("التالي ➡️" if ar else "Next ➡️"),
                                        callback_data=PointsCB(action="ledger", page=page + 1).pack()))
    return _kb([row] if row else [], lang)


def referral_menu(lang: str) -> InlineKeyboardMarkup:
    ar = is_ar(lang)
    return _kb(
        [
            [InlineKeyboardButton(text=("🔗 رابطي" if ar else "🔗 My link"),
                                  callback_data=ReferralCB(action="link").pack())],
            [InlineKeyboardButton(text=("📈 إحصائية دعواتي" if ar else "📈 My referral stats"),
                                  callback_data=ReferralCB(action="stats").pack())],
        ],
        lang,
    )


# ═══════════════════════════════════════════════════════════════════════════
#  settings
# ═══════════════════════════════════════════════════════════════════════════
def settings_menu(lang: str, *, tz: str = "UTC") -> InlineKeyboardMarkup:
    ar = is_ar(lang)
    return _kb(
        [
            [InlineKeyboardButton(text=("🌐 Language / اللغة" if ar else "🌐 اللغة / Language"),
                                  callback_data=SettingsCB(action="language").pack())],
            [InlineKeyboardButton(text=("⏰ التذكيرات" if ar else "⏰ Reminders"),
                                  callback_data=SettingsCB(action="reminders").pack())],
            [InlineKeyboardButton(text=("🎯 تغيير الهدف" if ar else "🎯 Change goal"),
                                  callback_data=SettingsCB(action="goal").pack())],
            [InlineKeyboardButton(text=(f"🕐 المنطقة الزمنية: {tz}" if ar else f"🕐 Timezone: {tz}"),
                                  callback_data=SettingsCB(action="timezone").pack())],
            [InlineKeyboardButton(text=("👤 تعديل بياناتي" if ar else "👤 Edit my profile"),
                                  callback_data=SettingsCB(action="profile").pack())],
            [InlineKeyboardButton(text=("🔒 الخصوصية وبياناتي" if ar else "🔒 Privacy & my data"),
                                  callback_data=SettingsCB(action="privacy").pack())],
        ],
        lang,
    )


def language_menu(lang: str) -> InlineKeyboardMarkup:
    return _kb(
        [[
            InlineKeyboardButton(text="🇸🇦 العربية", callback_data=SettingsCB(action="lang", arg="ar").pack()),
            InlineKeyboardButton(text="🇬🇧 English", callback_data=SettingsCB(action="lang", arg="en").pack()),
        ]],
        lang,
    )


def profile_fields(lang: str) -> InlineKeyboardMarkup:
    ar = is_ar(lang)
    fields = [
        ("age", "🎂 العمر" if ar else "🎂 Age"),
        ("weight", "⚖️ الوزن" if ar else "⚖️ Weight"),
        ("height", "📏 الطول" if ar else "📏 Height"),
        ("activity", "🚶 النشاط اليومي" if ar else "🚶 Activity level"),
        ("equipment", "🏋️ المعدات" if ar else "🏋️ Equipment"),
        ("budget", "💰 الميزانية" if ar else "💰 Budget"),
        ("food", "🍽️ نوع الأكل" if ar else "🍽️ Food style"),
        ("injuries", "⚠️ الإصابات" if ar else "⚠️ Injuries"),
    ]
    rows = [
        [InlineKeyboardButton(text=label, callback_data=SettingsCB(action="field", arg=key).pack())]
        for key, label in fields
    ]
    return _kb(rows, lang)


def reminders_menu(lang: str, rules: Sequence[Any]) -> InlineKeyboardMarkup:
    states = {rule.kind: rule for rule in rules}
    rows: list[list[InlineKeyboardButton]] = []
    for kind in REMINDER_LABELS:
        rule = states.get(kind)
        icon = "🟢" if (rule and rule.enabled) else "⚪️"
        rows.append([InlineKeyboardButton(
            text=f"{icon} {_label(REMINDER_LABELS, kind, lang)}",
            callback_data=ReminderCB(kind=kind, action="toggle").pack(),
        )])
        if rule and rule.kind == ReminderKind.WATER.value:
            rows.append([InlineKeyboardButton(
                text=("⏱️ كل ساعتين" if is_ar(lang) else "⏱️ Every 2 hours"),
                callback_data=ReminderCB(kind=kind, action="interval", arg="120").pack(),
            )])
    return _kb(rows, lang)


# ═══════════════════════════════════════════════════════════════════════════
#  admin
# ═══════════════════════════════════════════════════════════════════════════
def admin_menu(lang: str = "ar") -> InlineKeyboardMarkup:
    ar = is_ar(lang)
    return _kb(
        [
            [InlineKeyboardButton(text=("📊 الإحصائيات" if ar else "📊 Statistics"),
                                  callback_data=AdminCB(action="stats").pack())],
            [InlineKeyboardButton(text=("👥 المستخدمون" if ar else "👥 Users"),
                                  callback_data=AdminCB(action="users", page=1).pack()),
             InlineKeyboardButton(text=("🔍 بحث" if ar else "🔍 Search"),
                                  callback_data=AdminCB(action="search").pack())],
            [InlineKeyboardButton(text=("📢 رسالة جماعية" if ar else "📢 Broadcast"),
                                  callback_data=AdminCB(action="bc").pack())],
            [InlineKeyboardButton(text=("🤖 استهلاك API" if ar else "🤖 API usage"),
                                  callback_data=AdminCB(action="ai").pack()),
             InlineKeyboardButton(text=("🧾 السجل" if ar else "🧾 Audit"),
                                  callback_data=AdminCB(action="audit").pack())],
            [InlineKeyboardButton(text=("🔗 الاشتراك الإجباري" if ar else "🔗 Forced subscription"),
                                  callback_data=AdminCB(action="channels").pack())],
            [InlineKeyboardButton(text=("🧩 الموديلات والحدود" if ar else "🧩 Models & caps"),
                                  callback_data=AdminCB(action="config").pack())],
        ],
        lang,
    )


def admin_users_pager(lang: str, page: int, total_pages: int) -> InlineKeyboardMarkup:
    row: list[InlineKeyboardButton] = []
    if page > 1:
        row.append(InlineKeyboardButton(text="⬅️", callback_data=AdminCB(action="users", page=page - 1).pack()))
    row.append(InlineKeyboardButton(text=f"{page}/{max(1, total_pages)}", callback_data=AdminCB(action="users", page=page).pack()))
    if page < total_pages:
        row.append(InlineKeyboardButton(text="➡️", callback_data=AdminCB(action="users", page=page + 1).pack()))
    return _kb([row], lang)


def admin_user_card(lang: str, user_id: str, *, is_banned: bool) -> InlineKeyboardMarkup:
    ar = is_ar(lang)
    return _kb(
        [
            [InlineKeyboardButton(text=("📂 الملف الكامل" if ar else "📂 Full profile"),
                                  callback_data=AdminUserCB(action="profile", user_id=user_id).pack())],
            [InlineKeyboardButton(text=("➕ نقاط" if ar else "➕ Points"),
                                  callback_data=AdminUserCB(action="grant", user_id=user_id).pack()),
             InlineKeyboardButton(text=("➖ نقاط" if ar else "➖ Points"),
                                  callback_data=AdminUserCB(action="deduct", user_id=user_id).pack())],
            [InlineKeyboardButton(text=("🚫 حظر" if ar else "🚫 Ban") if not is_banned else ("✅ فك الحظر" if ar else "✅ Unban"),
                                  callback_data=AdminUserCB(action="unban" if is_banned else "ban", user_id=user_id).pack()),
             InlineKeyboardButton(text=("📅 اشتراك شهري" if ar else "📅 Monthly plan"),
                                  callback_data=AdminUserCB(action="sub", user_id=user_id).pack())],
            [InlineKeyboardButton(text=("🤖 استهلاكه" if ar else "🤖 Their usage"),
                                  callback_data=AdminUserCB(action="usage", user_id=user_id).pack()),
             InlineKeyboardButton(text=("📒 سجل نقاطه" if ar else "📒 Their ledger"),
                                  callback_data=AdminUserCB(action="ledger", user_id=user_id).pack())],
            [InlineKeyboardButton(text=("↩️ رجوع للقائمة" if ar else "↩️ Back to list"),
                                  callback_data=AdminCB(action="users", page=1).pack())],
        ],
        lang,
    )


def admin_broadcast(lang: str) -> InlineKeyboardMarkup:
    ar = is_ar(lang)
    return _kb(
        [
            [InlineKeyboardButton(text=("👥 الكل" if ar else "👥 Everyone"),
                                  callback_data=AdminCB(action="bc_set", arg="all").pack()),
             InlineKeyboardButton(text=("🔥 النشطون" if ar else "🔥 Active"),
                                  callback_data=AdminCB(action="bc_set", arg="active").pack()),],
            [InlineKeyboardButton(text=("🏆 مشتركو الكوتش" if ar else "🏆 Coach users"),
                                  callback_data=AdminCB(action="bc_set", arg="coaches").pack()),
             InlineKeyboardButton(text=("📜 السجل" if ar else "📜 History"),
                                  callback_data=AdminCB(action="bc_history").pack())],
        ],
        lang,
    )


def admin_channels(lang: str, enabled: bool) -> InlineKeyboardMarkup:
    ar = is_ar(lang)
    return _kb(
        [
            [InlineKeyboardButton(text=("🟢 مفعّل — إيقاف" if ar else "🟢 Enabled — turn off") if enabled
                                  else ("⚪️ متوقف — تفعيل" if ar else "⚪️ Off — enable"),
                                  callback_data=AdminCB(action="channels_toggle").pack())],
            [InlineKeyboardButton(text=("➕ أضف/عدّل القنوات" if ar else "➕ Add/edit channels"),
                                  callback_data=AdminCB(action="channels_edit").pack())],
        ],
        lang,
    )


def admin_config(lang: str) -> InlineKeyboardMarkup:
    ar = is_ar(lang)
    return _kb(
        [
            [InlineKeyboardButton(text=("🧩 توزيع الموديلات" if ar else "🧩 Model routing"),
                                  callback_data=AdminCB(action="cfg_models").pack())],
            [InlineKeyboardButton(text=("💵 أسعار النقاط" if ar else "💵 Point prices"),
                                  callback_data=AdminCB(action="cfg_points").pack())],
            [InlineKeyboardButton(text=("🛑 حدود الاستخدام" if ar else "🛑 Usage caps"),
                                  callback_data=AdminCB(action="cfg_caps").pack())],
            [InlineKeyboardButton(text=("📅 الاشتراكات المستحقة" if ar else "📅 Due subscriptions"),
                                  callback_data=AdminCB(action="subs_run").pack())],
        ],
        lang,
    )


def admin_goal_filter(lang: str) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for goal in all_goals(lang):
        row.append(InlineKeyboardButton(text=goal["label"][:22],
                                        callback_data=AdminCB(action="users_goal", arg=goal["key"]).pack()))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return _kb(rows, lang)


def plan_kind_label(kind: str, lang: str) -> str:
    table = {
        "training": ("🏋️ تمارين", "🏋️ Training"),
        "nutrition": ("🍽️ وجبات", "🍽️ Nutrition"),
        "boxing": ("🥊 ملاكمة", "🥊 Boxing"),
        "flexibility": ("🧘 مرونة", "🧘 Mobility"),
    }
    return _label(table, kind, lang)


def subscription_label(plan: str, lang: str) -> str:
    table = {
        SubscriptionPlan.NONE.value: ("لا يوجد", "None"),
        SubscriptionPlan.MONTHLY.value: ("شهري", "Monthly"),
        SubscriptionPlan.QUARTERLY.value: ("ربع سنوي", "Quarterly"),
    }
    return _label(table, plan, lang)


__all__ = [n for n in dir() if not n.startswith("_")]
