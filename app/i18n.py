"""Arabic-first i18n (spec §5.7 — عربي/إنجليزي على الأقل).

A single flat catalogue with ``{key: {"ar": …, "en": …}}`` and ``**kwargs``
formatting.  Adding a language = adding one value per key + a ``Language``
member; nothing else changes.
"""
from __future__ import annotations

import re
from typing import Any

from app.constants import Language

Catalogue = dict[str, dict[str, str]]

STRINGS: Catalogue = {
    # ── generic ──────────────────────────────────────────────────────────────
    "app.name": {"ar": "الكوتش الذكي 🏋️", "en": "AI Coach 🏋️"},
    "common.back": {"ar": "↩️ رجوع", "en": "↩️ Back"},
    "common.cancel": {"ar": "❌ إلغاء", "en": "❌ Cancel"},
    "common.confirm": {"ar": "✅ تأكيد", "en": "✅ Confirm"},
    "common.close": {"ar": "إغلاق", "en": "Close"},
    "common.main_menu": {"ar": "🏠 القائمة الرئيسية", "en": "🏠 Main menu"},
    "common.next": {"ar": "التالي ⬅️", "en": "Next ➡️"},
    "common.yes": {"ar": "نعم", "en": "Yes"},
    "common.no": {"ar": "لا", "en": "No"},
    "common.skip": {"ar": "تخطي", "en": "Skip"},
    "common.done": {"ar": "تم", "en": "Done"},
    "common.error": {"ar": "حدث خطأ غير متوقع. حاول مرة أخرى.", "en": "Unexpected error. Please try again."},
    "common.ai_busy": {
        "ar": "الكوتش مشغول الآن أو تأخر المزود بالرد. أعد المحاولة بعد قليل.",
        "en": "The coach is busy or the provider is slow. Try again in a moment.",
    },
    "common.ai_no_key": {
        "ar": "⚠️ لم يتم ضبط مفتاح الذكاء الاصطناعي (NANOGPT_API_KEY) على الخادم.",
        "en": "⚠️ The AI key (NANOGPT_API_KEY) is not configured on the server.",
    },

    # ── main menu ────────────────────────────────────────────────────────────
    "menu.title": {
        "ar": "<b>أهلًا {name} 👋</b>\n\nشنو تريد نسوي اليوم؟",
        "en": "<b>Welcome {name} 👋</b>\n\nWhat do you want to work on today?",
    },
    "menu.consult": {"ar": "💬 الاستشارة العامة (مجانية)", "en": "💬 General consultation (free)"},
    "menu.coach": {"ar": "🏆 تدريب شخصي متابع 24 ساعة", "en": "🏆 24h personal coaching"},
    "menu.calories": {"ar": "🍽️ حساب سعرات الأكل (صورة)", "en": "🍽️ Food calorie scan (photo)"},
    "menu.progress": {"ar": "📈 تقدمي وقياساتي", "en": "📈 My progress & measurements"},
    "menu.plan": {"ar": "📋 برنامجي الحالي", "en": "📋 My current programme"},
    "menu.points": {"ar": "🪙 نقاطي", "en": "🪙 My points"},
    "menu.referral": {"ar": "🎁 دعوة صديق", "en": "🎁 Invite a friend"},
    "menu.settings": {"ar": "⚙️ الإعدادات", "en": "⚙️ Settings"},
    "menu.admin": {"ar": "🛡️ لوحة الأدمن", "en": "🛡️ Admin panel"},
    "menu.status_line": {
        "ar": "🪙 نقاطك: <b>{points}</b> | 🔥 الالتزام: <b>{streak}</b> يوم | 🎯 {goal}",
        "en": "🪙 Points: <b>{points}</b> | 🔥 Streak: <b>{streak}</b> days | 🎯 {goal}",
    },
    "menu.today_line": {
        "ar": "🍽️ اليوم: {eaten}/{target} سعرة — متبقي <b>{left}</b> | 💧 {water}/{water_target} مل",
        "en": "🍽️ Today: {eaten}/{target} kcal — <b>{left}</b> left | 💧 {water}/{water_target} ml",
    },

    # ── onboarding ───────────────────────────────────────────────────────────
    "onb.welcome": {
        "ar": (
            "<b>أهلًا فيك 👋</b>\n\n"
            "أنا كوتش رياضي وتغذية بخبرة تتجاوز 25 سنة — مو بوت ردود جاهزة.\n"
            "بني خطتك على أرقامك أنت: عمرك، وزنك، هدفك، معداتك، إصاباتك وميزانيتك.\n\n"
            "خلنا نبلش. أول شي: <b>اسمك</b> (اللي تحب أناديك فيه)."
        ),
        "en": (
            "<b>Welcome 👋</b>\n\nI'm a strength & nutrition coach with 25+ years of experience — not a canned-reply bot.\n"
            "I build your plan from your own numbers: age, weight, goal, equipment, injuries and budget.\n\n"
            "Let's start: <b>your name</b> (what should I call you?)."
        ),
    },
    "onb.ask_age": {
        "ar": "كم <b>عمرك</b>؟ (اكتب رقمًا فقط، مثال: 27)",
        "en": "How <b>old</b> are you? (number only, e.g. 27)",
    },
    "onb.ask_gender": {"ar": "<b>جنسك</b>؟", "en": "Your <b>sex</b>?"},
    "onb.ask_height": {
        "ar": "كم <b>طولك</b> بالسنتيمتر؟ (مثال: 175)",
        "en": "Your <b>height</b> in cm? (e.g. 175)",
    },
    "onb.ask_weight": {
        "ar": "كم <b>وزنك الحالي</b> بالكيلوغرام؟ (مثال: 82.5)",
        "en": "Your current <b>weight</b> in kg? (e.g. 82.5)",
    },
    "onb.ask_target_weight": {
        "ar": "إيش <b>الوزن</b> اللي تبي توصله؟ (اكتب «تخطي» إذا هدفك مو رقمًا)",
        "en": "What <b>weight</b> do you want to reach? (send “skip” if it isn't a number)",
    },
    "onb.ask_activity": {"ar": "<b>نشاطك اليومي</b>؟", "en": "Your <b>daily activity</b>?"},
    "onb.ask_goal": {"ar": "إيش <b>هدفك</b>؟ (تقدر تغيّره لاحقًا في أي وقت)", "en": "What's your <b>goal</b>? (changeable later)"},
    "onb.ask_goal_custom": {
        "ar": "اكتب هدفك بكلامك بالتفصيل (كل ما كنت أدق، كل ما كانت الخطة أحسن).",
        "en": "Describe your goal in your own words (the more precise, the better the plan).",
    },
    "onb.ask_equipment": {"ar": "إيش <b>المعدات</b> المتوفرة عندك؟", "en": "What <b>equipment</b> do you have?"},
    "onb.ask_injuries": {
        "ar": (
            "<b>⚠️ مهم — الإصابات والموانع الصحية</b>\n"
            "اكتب أي إصابة أو حالة صحية (ركبة، ظهر، كتف، ضغط، سكري، عملية سابقة…).\n"
            "إذا ما عندك شيء اكتب «لا شيء»."
        ),
        "en": (
            "<b>⚠️ Important — injuries & conditions</b>\n"
            "List any injury or health condition (knee, back, shoulder, blood pressure, diabetes, past surgery…).\n"
            "If none, type “none”."
        ),
    },
    "onb.ask_budget": {"ar": "<b>ميزانيتك</b> للأكل؟", "en": "Your food <b>budget</b>?"},
    "onb.ask_food_style": {
        "ar": "إيش <b>نوع الأكل</b> اللي تأكله عادة؟ (شعبي/بيتي/مطاعم… واكتب أمثلة تحبها)",
        "en": "What <b>food</b> do you usually eat? (local/home/restaurant… name foods you like)",
    },
    "onb.ask_disliked": {
        "ar": "أكل <b>ما تحبه</b> أو عندك حساسية منه؟ (اكتب «لا شيء» للتخطي)",
        "en": "Foods you <b>dislike</b> or are allergic to? (send “none” to skip)",
    },
    "onb.invalid_number": {
        "ar": "رقم غير صحيح. اكتب رقمًا فقط (مثال: {example}).",
        "en": "Invalid number. Send a number only (e.g. {example}).",
    },
    "onb.invalid_age": {"ar": "العمر يجب أن يكون بين 12 و 90.", "en": "Age must be between 12 and 90."},
    "onb.invalid_height": {"ar": "الطول يجب أن يكون بين 120 و 230 سم.", "en": "Height must be between 120 and 230 cm."},
    "onb.invalid_weight": {"ar": "الوزن يجب أن يكون بين 30 و 300 كغ.", "en": "Weight must be between 30 and 300 kg."},
    "onb.complete": {
        "ar": (
            "✅ <b>تم إنشاء ملفك</b>\n\n{plan}\n\n"
            "🎯 خطة التمارين جاهزة للتوليد من زر «برنامجي الحالي».\n"
            "🍽️ وللوجبات: أرسل صورة أكلك في أي وقت وسأحسبها وأخصمها من يومك."
        ),
        "en": (
            "✅ <b>Profile created</b>\n\n{plan}\n\n"
            "🎯 Generate your training programme from “My current programme”.\n"
            "🍽️ For meals: send a photo of your food any time and I'll log it against your day."
        ),
    },
    "onb.safety_warning": {
        "ar": (
            "🔴 <b>تنبيه سلامة</b>\n{detail}\n\n"
            "بناءً على ما ذكرته: {referral}\n"
            "التمارين المستبعدة تلقائيًا: {restricted}\n"
            "البدائل الآمنة: {alternatives}"
        ),
        "en": (
            "🔴 <b>Safety notice</b>\n{detail}\n\nBased on what you reported: {referral}\n"
            "Automatically excluded: {restricted}\nSafe alternatives: {alternatives}"
        ),
    },
    "onb.already_done": {
        "ar": "ملفك مكتمل مسبقًا. تقدر تعدّل بياناتك من ⚙️ الإعدادات.",
        "en": "Your profile is already complete. Edit it from ⚙️ Settings.",
    },

    # ── privacy ──────────────────────────────────────────────────────────────
    "privacy.title": {"ar": "🔒 سياسة الخصوصية", "en": "🔒 Privacy policy"},
    "privacy.body": {
        "ar": (
            "<b>وش نخزّن عنك؟</b>\n"
            "• بياناتك: الاسم، العمر، الجنس، الطول، الوزن، هدفك، إصاباتك وميزانيتك.\n"
            "• سجلك اليومي: الوجبات والسعرات والماكروز والماء والتمارين.\n"
            "• <b>صورك</b>: صور الأكل وصور تقدم الجسم — تُخزَّن مشفّرة النقل، ولا يشوفها أحد غيرك والكوتش الآلي.\n"
            "• استهلاك الذكاء الاصطناعي: عدد التوكنز والتكلفة لمراقبة الجودة.\n\n"
            "<b>وش ما نسويه؟</b>\n"
            "• ما نبيع بياناتك ولا نشاركها مع أي طرف ثالث إلا مزود الذكاء الاصطناعي لمعالجة رسالتك/صورتك.\n"
            "• ما نحلّل وجهك ولا نحفظ هويتك من الصور.\n\n"
            "<b>حقوقك:</b>\n"
            "• تصدير كامل بياناتك (PDF) في أي وقت.\n"
            "• حذف صورك أو كل بياناتك نهائيًا من ⚙️ الإعدادات.\n"
            "• الصور تُحذف تلقائيًا بعد {retention} يومًا."
        ),
        "en": (
            "<b>What we store</b>\n"
            "• Profile: name, age, sex, height, weight, goal, injuries, budget.\n"
            "• Daily log: meals, calories, macros, water, workouts.\n"
            "• <b>Your photos</b>: food and body-progress images — encrypted in transit, seen only by you and the AI coach.\n"
            "• AI usage: token counts and cost for quality monitoring.\n\n"
            "<b>What we never do</b>\n"
            "• We don't sell or share your data, except with the AI provider needed to process your message/image.\n"
            "• We never analyse your face or store an identity from photos.\n\n"
            "<b>Your rights</b>\n"
            "• Export everything as a PDF at any time.\n"
            "• Delete your photos or all data from ⚙️ Settings.\n"
            "• Photos are purged automatically after {retention} days."
        ),
    },
    "privacy.ask": {
        "ar": "للمتابعة يجب الموافقة على سياسة الخصوصية. توافق؟",
        "en": "You must accept the privacy policy to continue. Do you accept?",
    },
    "privacy.accepted": {"ar": "✅ تم تسجيل موافقتك (الإصدار {version}).", "en": "✅ Consent recorded (version {version})."},
    "privacy.declined": {
        "ar": "بدون موافقتك لا نقدر نخزّن بياناتك، فالبوت سيتوقف هنا. راجعنا متى ما كنت جاهزًا.",
        "en": "Without consent we can't store your data, so the bot stops here. Come back when you're ready.",
    },
    "privacy.deleted": {"ar": "🗑️ تم حذف {count} عنصر من بياناتك.", "en": "🗑️ Deleted {count} items of your data."},

    # ── forced subscription ──────────────────────────────────────────────────
    "forced.title": {"ar": "📢 اشترك أولًا", "en": "📢 Subscribe first"},
    "forced.body": {
        "ar": "لاستخدام البوت لازم تشترك بقناتنا أولًا:\n{channels}\n\nبعدها اضغط «تحقّق».",
        "en": "To use the bot you must join our channel(s) first:\n{channels}\n\nThen press “Verify”.",
    },
    "forced.check": {"ar": "🔄 تحقّق", "en": "🔄 Verify"},
    "forced.ok": {"ar": "✅ تمام، أنت مشترك. أهلًا فيك!", "en": "✅ Verified. Welcome!"},
    "forced.fail": {"ar": "❌ لم أتمكن من تأكيد اشتراكك بعد. اشترك ثم اضغط «تحقّق».", "en": "❌ Not verified yet. Join then press “Verify”."},

    # ── consult ──────────────────────────────────────────────────────────────
    "consult.intro": {
        "ar": (
            "<b>💬 الاستشارة العامة</b> — مجانية ومفتوحة لأي سؤال رياضي أو تغذوي.\n\n"
            "جرّب تسأل:\n"
            "• «أعطني تمارين تحرق 300 سعرة»\n"
            "• «تمارين بيت بدون معدات للصدر»\n"
            "• «كيف أبلش ملاكمة من البيت؟»\n"
            "• «تمارين استطالة لزيادة الطول»\n"
            "• «كم سعرة في 200 غرام رز مطبوخ؟»\n\n"
            "اكتب سؤالك الآن 👇 (هذه جلسة لحظية — ما تُحفظ في تقدمك)"
        ),
        "en": (
            "<b>💬 General consultation</b> — free, any fitness/nutrition question.\n\n"
            "Try:\n• “Exercises that burn 300 kcal”\n• “No-equipment home chest workout”\n"
            "• “How do I start boxing at home?”\n• “Stretching routine for height”\n"
            "• “Calories in 200 g of cooked rice?”\n\nType your question 👇 (stateless — not saved to your progress)"
        ),
    },
    "consult.limit": {
        "ar": "وصلت حد الاستشارات المجانية اليوم ({limit}). رجاءً جرّب بكرة أو افتح الكوتش الشخصي للمتابعة غير المحدودة.",
        "en": "You hit today's free consultation limit ({limit}). Try again tomorrow or open the personal coach.",
    },
    "consult.ask_question": {"ar": "اكتب سؤالك الرياضي/التغذوي 👇", "en": "Type your fitness/nutrition question 👇"},

    # ── coach ────────────────────────────────────────────────────────────────
    "coach.locked": {
        "ar": (
            "🔒 <b>الكوتش الشخصي 24 ساعة</b> يحتاج <b>{cost} نقطة</b>.\n\n"
            "وش تحصل؟\n"
            "• متابعة يومية كاملة (وزن، أكل، تمارين، التزام)\n"
            "• تعديل سعراتك وماكروزك تلقائيًا حسب تقدمك الفعلي\n"
            "• خصم صور أكلك من رصيد يومك فورًا\n"
            "• إعادة حساب يومك إذا فوّت وجبة\n"
            "• تقارير أسبوعية وشهرية + رسم بياني\n\n"
            "رصيدك الحالي: <b>{balance}</b> نقطة"
        ),
        "en": (
            "🔒 The <b>24h personal coach</b> costs <b>{cost} points</b>.\n\n"
            "You get:\n• Full daily tracking (weight, food, training, adherence)\n"
            "• Automatic calorie/macro adjustment from your real progress\n"
            "• Food photos deducted from today instantly\n• Instant day recalculation when you skip a meal\n"
            "• Weekly/monthly reports + charts\n\nYour balance: <b>{balance}</b> points"
        ),
    },
    "coach.unlock": {"ar": "🔓 افتح الكوتش ({cost} نقطة)", "en": "🔓 Unlock coach ({cost} points)"},
    "coach.unlocked": {
        "ar": "✅ <b>تم فتح الكوتش الشخصي!</b> صلاحيتك حتى: {until}\n\nابدأ بإرسال وزنك اليوم أو صورة أكلك.",
        "en": "✅ <b>Personal coach unlocked!</b> Valid until: {until}\n\nStart by sending today's weight or a food photo.",
    },
    "coach.intro": {
        "ar": (
            "<b>🏆 الكوتش الشخصي — وضع المتابعة</b>\n\n"
            "أنا معك 24 ساعة. اكتب لي أي شيء:\n"
            "• «وزني اليوم 81.3»\n"
            "• «ما أكلت الغدا» أو «ما كملت بروتيني»\n"
            "• «أعطني وجبة بـ 400 سعرة من أكلنا»\n"
            "• «ما قدرت أتمرّن اليوم»\n"
            "أو أرسل صورة أكلك مباشرة.\n\n"
            "{status}"
        ),
        "en": (
            "<b>🏆 Personal coach — tracking mode</b>\n\nI'm with you 24h. Tell me anything:\n"
            "• “Today's weight 81.3”\n• “I skipped lunch” or “I missed my protein”\n"
            "• “Give me a 400 kcal meal from my local food”\n• “I couldn't train today”\n"
            "or send a photo of your food.\n\n{status}"
        ),
    },
    "coach.insufficient": {
        "ar": "🪙 نقاطك ما تكفي لهذه الرسالة (تحتاج ~{need}، عندك {have}). احصل على نقاط من «🪙 نقاطي» أو بدعوة صديق.",
        "en": "🪙 Not enough points for this message (needs ~{need}, you have {have}). Top up from “🪙 Points” or invite a friend.",
    },
    "coach.daily_limit": {
        "ar": "🛑 وصلت حد رسائل الكوتش اليوم ({limit} رسالة). هذا الحد لحماية رصيدك ورصيد الخدمة. راجعني بكرة.",
        "en": "🛑 You reached today's coach message limit ({limit}). This protects your balance and the service. See you tomorrow.",
    },
    "coach.charged": {"ar": "🪙 خُصم {points} نقطة — رصيدك {balance}.", "en": "🪙 Charged {points} points — balance {balance}."},
    "coach.thinking": {"ar": "🤔 أحلل وضعك…", "en": "🤔 Analysing your situation…"},

    # ── food scan ────────────────────────────────────────────────────────────
    "scan.intro": {
        "ar": (
            "<b>🍽️ حساب سعرات الأكل من الصورة</b>\n\n"
            "أرسل صورة وجبتك وسأحللها: الأصناف، الكميات التقديرية، السعرات والماكروز.\n"
            "إذا كان في غموض (كمية الأرز؟ نوع الزيت؟) بسألك سؤال أو اثنين محددين بدل التخمين.\n\n"
            "🎁 أول {free} صور يوميًا مجانية، بعدها {cost} نقاط للصورة.\n"
            "ملاحظة: أرسل الصورة <b>بدون ضغط</b> (كـ File) إذا كانت الكمية صغيرة في الطبق."
        ),
        "en": (
            "<b>🍽️ Food calorie scan from a photo</b>\n\nSend a photo of your meal and I'll break it down: items, "
            "estimated portions, calories and macros.\nIf something is genuinely ambiguous (rice amount? oil type?) "
            "I'll ask one or two specific questions instead of guessing.\n\n"
            "🎁 First {free} scans/day are free, then {cost} points each.\n"
            "Tip: send the photo <b>uncompressed</b> (as a File) for small portions."
        ),
    },
    "scan.send_photo": {"ar": "📸 أرسل صورة الوجبة الآن", "en": "📸 Send the meal photo now"},
    "scan.not_food": {
        "ar": "ما قدرت أتعرف على أكل في هذه الصورة 🤔 أرسل صورة أوضح للوجبة من فوق وبإضاءة جيدة.",
        "en": "I couldn't find food in this photo 🤔 Send a clearer, well-lit top-down shot of the meal.",
    },
    "scan.result": {
        "ar": (
            "<b>🍽️ {meal}</b>\n\n{items}\n\n<b>الإجمالي: {calories} سعرة</b>\n"
            "بروتين {protein} جم | كارب {carbs} جم | دهون {fats} جم\n\n"
            "الثقة: {confidence}%{assumptions}"
        ),
        "en": (
            "<b>🍽️ {meal}</b>\n\n{items}\n\n<b>Total: {calories} kcal</b>\n"
            "P {protein} g | C {carbs} g | F {fats} g\n\nConfidence: {confidence}%{assumptions}"
        ),
    },
    "scan.item_line": {
        "ar": "• {name} (~{grams} جم، {method}): {calories} سعرة",
        "en": "• {name} (~{grams} g, {method}): {calories} kcal",
    },
    "scan.assumptions": {"ar": "\n\n<b>افتراضات التقدير:</b>\n{list}", "en": "\n\n<b>Assumptions:</b>\n{list}"},
    "scan.clarify": {
        "ar": "<b>❓ قبل ما أحسب بدقة، محتاج أوضح:</b>\n{questions}\n\nجاوبني بأرقام/كلمات قصيرة.",
        "en": "<b>❓ Before I calculate precisely, I need to clarify:</b>\n{questions}\n\nAnswer with short numbers/words.",
    },
    "scan.logged": {
        "ar": "✅ سُجّلت الوجبة في يومك.\n\n<b>المتبقي اليوم:</b> {left} سعرة\nبروتين {p} جم | كارب {c} جم | دهون {f} جم\n💧 ماء: {water}/{water_target} مل",
        "en": "✅ Meal logged.\n\n<b>Left today:</b> {left} kcal\nP {p} g | C {c} g | F {f} g\n💧 Water: {water}/{water_target} ml",
    },
    "scan.limit": {
        "ar": "🛑 وصلت حد تحليل الصور اليوم ({limit}). جرّب بكرة.",
        "en": "🛑 You reached today's image-analysis limit ({limit}). Try tomorrow.",
    },

    # ── progress ─────────────────────────────────────────────────────────────
    "prog.title": {"ar": "<b>📈 تقدمك</b>", "en": "<b>📈 Your progress</b>"},
    "prog.weight": {"ar": "⚖️ تسجيل الوزن", "en": "⚖️ Log weight"},
    "prog.measure": {"ar": "📏 القياسات (خصر/صدر/ذراع)", "en": "📏 Measurements (waist/chest/arm)"},
    "prog.photo": {"ar": "📷 صورة تقدم الجسم", "en": "📷 Body progress photo"},
    "prog.chart": {"ar": "📉 الرسم البياني للوزن", "en": "📉 Weight chart"},
    "prog.report_week": {"ar": "🗓️ التقرير الأسبوعي", "en": "🗓️ Weekly report"},
    "prog.report_month": {"ar": "📆 التقرير الشهري", "en": "📆 Monthly report"},
    "prog.export_pdf": {"ar": "📄 تصدير رحلتي PDF", "en": "📄 Export my journey (PDF)"},
    "prog.ask_weight": {"ar": "اكتب وزنك اليوم بالكيلوغرام (مثال: 81.4)", "en": "Type today's weight in kg (e.g. 81.4)"},
    "prog.weight_saved": {
        "ar": "✅ سُجّل وزنك: <b>{weight} كغ</b>\nالتغيّر عن آخر قياس: {delta}\nعن البداية: {total}",
        "en": "✅ Weight saved: <b>{weight} kg</b>\nChange since last: {delta}\nSince start: {total}",
    },
    "prog.ask_measurements": {
        "ar": (
            "اكتب القياسات بالسنتيمتر، كل واحدة بسطر (اترك ما لا تعرفه):\n"
            "خصر: \nصدر: \nذراع: \nفخذ: \nورك: \nرقبة:\n\nمثال:\nخصر: 88\nذراع: 34"
        ),
        "en": (
            "Write measurements in cm, one per line (skip what you don't know):\n"
            "waist: \nchest: \narm: \nthigh: \nhip: \nneck:\n\nExample:\nwaist: 88\narm: 34"
        ),
    },
    "prog.measurements_saved": {"ar": "✅ حُفظت القياسات: {list}", "en": "✅ Measurements saved: {list}"},
    "prog.ask_photo": {
        "ar": (
            "📷 أرسل صورة تقدم (أمام/جنب/خلف).\n"
            "نصائح لدقة التحليل: إضاءة ثابتة، نفس المكان والوضعية، ملابس متشابهة، بدون فلاتر.\n"
            "لن نحلل وجهك ولن نخمّن وزنًا دقيقًا — فقط تكوين الجسم والوقفة."
        ),
        "en": (
            "📷 Send a progress photo (front/side/back).\n"
            "For accurate analysis: consistent light, same spot and pose, similar clothing, no filters.\n"
            "We never analyse your face and won't claim an exact weight — only composition and posture."
        ),
    },
    "prog.photo_angle": {"ar": "من أي زاوية هذه الصورة؟", "en": "Which angle is this photo?"},
    "prog.no_data": {"ar": "لا توجد بيانات كافية بعد — سجّل وزنك أولًا.", "en": "Not enough data yet — log your weight first."},
    "prog.photo_saved": {"ar": "✅ حُفظت الصورة في سجل تقدمك ({angle}).", "en": "✅ Photo saved to your progress log ({angle})."},

    # ── points / referral / subscription ─────────────────────────────────────
    "points.title": {
        "ar": "<b>🪙 نقاطك: {balance}</b>\n\nإجمالي المكتسب: {earned} | المصروف: {spent}\n\n<b>وش تصرف عليه؟</b>\n{prices}",
        "en": "<b>🪙 Your points: {balance}</b>\n\nEarned: {earned} | Spent: {spent}\n\n<b>What they pay for</b>\n{prices}",
    },
    "points.prices_line": {"ar": "• {label}: {cost}", "en": "• {label}: {cost}"},
    "points.ledger_title": {"ar": "<b>📒 سجل النقاط (آخر {n})</b>", "en": "<b>📒 Points ledger (last {n})</b>"},
    "points.get_more": {"ar": "➕ احصل على نقاط", "en": "➕ Get more points"},
    "points.contact_admin": {
        "ar": "شحن النقاط يتم حاليًا عبر الإدارة{contact}. أرسل له معرّفك: <code>{tg_id}</code>",
        "en": "Points are currently topped up by the admin{contact}. Send them your ID: <code>{tg_id}</code>",
    },
    "points.streak_reward": {"ar": "🔥 مكافأة التزام {days} يوم: +{points} نقطة!", "en": "🔥 {days}-day streak reward: +{points} points!"},
    "ref.title": {
        "ar": (
            "<b>🎁 دعوة صديق = نقاط مجانية</b>\n\n"
            "رابطك:\n<code>{link}</code>\n\n"
            "كودك: <code>{code}</code>\n"
            "• كل صديق يسجّل: <b>+{inviter} نقطة</b> لك و <b>+{invitee}</b> له\n"
            "• كل {every} أصدقاء: مكافأة <b>+{bonus} نقطة</b>\n\n"
            "دعواتك حتى الآن: <b>{count}</b>"
        ),
        "en": (
            "<b>🎁 Invite a friend = free points</b>\n\nYour link:\n<code>{link}</code>\n\nYour code: <code>{code}</code>\n"
            "• Each friend who signs up: <b>+{inviter}</b> for you, <b>+{invitee}</b> for them\n"
            "• Every {every} friends: bonus <b>+{bonus} points</b>\n\nYour referrals so far: <b>{count}</b>"
        ),
    },
    "ref.welcome_referred": {
        "ar": "🎁 صديقك {name} دعاك — حصلت على <b>{points} نقطة</b> هدية ترحيبية!",
        "en": "🎁 {name} invited you — you got <b>{points} points</b> as a welcome gift!",
    },
    "ref.rewarded": {
        "ar": "✅ صديقك انضم! حصلت على <b>+{points} نقطة</b> (إجمالي دعواتك: {count}).",
        "en": "✅ Your friend joined! You earned <b>+{points} points</b> (total referrals: {count}).",
    },
    "sub.title": {"ar": "<b>📅 اشتراكك</b>\n{status}", "en": "<b>📅 Your subscription</b>\n{status}"},
    "sub.none": {
        "ar": "لا يوجد اشتراك نشط. الاشتراك الشهري يضيف <b>{points} نقطة</b> تلقائيًا كل 30 يومًا. للتفعيل تواصل مع الإدارة.",
        "en": "No active subscription. The monthly plan adds <b>{points} points</b> automatically every 30 days. Contact the admin to activate.",
    },
    "sub.active": {"ar": "✅ {plan} — ينتهي: {until}\nنقاط الدورة القادمة: +{points}", "en": "✅ {plan} — expires: {until}\nNext cycle: +{points} points"},

    # ── settings / reminders / language ──────────────────────────────────────
    "set.title": {"ar": "<b>⚙️ الإعدادات</b>", "en": "<b>⚙️ Settings</b>"},
    "set.language": {"ar": "🌐 اللغة / Language", "en": "🌐 اللغة / Language"},
    "set.reminders": {"ar": "⏰ التذكيرات", "en": "⏰ Reminders"},
    "set.profile": {"ar": "👤 تعديل ملفي", "en": "👤 Edit my profile"},
    "set.goal": {"ar": "🎯 تغيير الهدف", "en": "🎯 Change goal"},
    "set.timezone": {"ar": "🕐 المنطقة الزمنية: {tz}", "en": "🕐 Timezone: {tz}"},
    "set.privacy": {"ar": "🔒 الخصوصية وبياناتي", "en": "🔒 Privacy & my data"},
    "set.export": {"ar": "📤 تصدير بياناتي", "en": "📤 Export my data"},
    "set.delete_photos": {"ar": "🗑️ حذف كل صوري", "en": "🗑️ Delete all my photos"},
    "set.delete_all": {"ar": "☠️ حذف حسابي وبياناتي", "en": "☠️ Delete my account & data"},
    "set.saved": {"ar": "✅ تم الحفظ.", "en": "✅ Saved."},
    "rem.title": {
        "ar": "<b>⏰ التذكيرات</b>\n\n{list}\n\nاختر نوع التذكير لتفعيله/تعديله:",
        "en": "<b>⏰ Reminders</b>\n\n{list}\n\nPick a reminder to enable/edit:",
    },
    "rem.water": {"ar": "💧 شرب الماء", "en": "💧 Drink water"},
    "rem.workout": {"ar": "🏋️ موعد التمرين", "en": "🏋️ Workout time"},
    "rem.weigh_in": {"ar": "⚖️ تسجيل الوزن الأسبوعي", "en": "⚖️ Weekly weigh-in"},
    "rem.meal_log": {"ar": "🍽️ تسجيل الوجبات", "en": "🍽️ Log your meals"},
    "rem.progress_photo": {"ar": "📷 صورة تقدم شهرية", "en": "📷 Monthly progress photo"},
    "rem.coach_checkin": {"ar": "🤝 متابعة الكوتش اليومية", "en": "🤝 Daily coach check-in"},
    "rem.on": {"ar": "✅ فعّلت تذكير {kind}. سأرسله في {time}.", "en": "✅ {kind} reminder on. I'll send it at {time}."},
    "rem.off": {"ar": "🔕 أوقفت تذكير {kind}.", "en": "🔕 {kind} reminder off."},
    "rem.water_msg": {"ar": "💧 وقت الماء! الهدف اليوم {target} مل — شربت {done} مل. باقي {left} مل.", "en": "💧 Water time! Target {target} ml — you had {done} ml. {left} ml left."},
    "rem.workout_msg": {"ar": "🏋️ موعد تمرين اليوم: {title}\nما عندك عذر — {days} يوم التزامك الحالي.", "en": "🏋️ Today's session: {title}\nNo excuses — your streak is {days} days."},
    "rem.weigh_msg": {"ar": "⚖️ يوم الميزان! أرسل وزنك الآن (بعد الاستيقاظ، قبل الأكل) لأعدّل سعراتك إذا لزم.", "en": "⚖️ Weigh-in day! Send your weight now (after waking, before eating) so I can adjust your calories."},
    "rem.meal_msg": {"ar": "🍽️ باقي لك {left} سعرة و {protein} جم بروتين اليوم. سجل وجبتك أو أرسل صورة.", "en": "🍽️ {left} kcal and {protein} g protein left today. Log a meal or send a photo."},
    "rem.photo_msg": {"ar": "📷 مرّت {days} أيام بدون صورة تقدم. صورة اليوم = مقارنة دقيقة بعد شهر.", "en": "📷 It's been {days} days without a progress photo. Today's photo = an accurate comparison next month."},
    "rem.checkin_msg": {"ar": "🤝 كيف كان يومك؟ التمرين؟ الأكل؟ أعطني تحديثًا سريعًا وأعدّل لك الخطة.", "en": "🤝 How was your day? Training? Food? Give me a quick update and I'll adjust the plan."},

    # ── plan / programmes ────────────────────────────────────────────────────
    "plan.title": {"ar": "<b>📋 برنامجك الحالي</b>\n{title}", "en": "<b>📋 Current programme</b>\n{title}"},
    "plan.none": {"ar": "ما عندك برنامج بعد. ولّد واحدًا الآن؟", "en": "No programme yet. Generate one now?"},
    "plan.generate_training": {"ar": "🏋️ توليد برنامج تمارين", "en": "🏋️ Generate training programme"},
    "plan.generate_nutrition": {"ar": "🍽️ توليد خطة وجبات", "en": "🍽️ Generate meal plan"},
    "plan.generate_boxing": {"ar": "🥊 برنامج ملاكمة مرحلي", "en": "🥊 Phased boxing programme"},
    "plan.generate_flexibility": {"ar": "🧘 برنامج استطالة ومرونة", "en": "🧘 Stretching & mobility programme"},
    "plan.generating": {"ar": "⏳ أبني برنامجك على بياناتك… هذا يأخذ ~30 ثانية.", "en": "⏳ Building your programme from your data… ~30 seconds."},
    "plan.saved": {"ar": "✅ حُفظ البرنامج: <b>{title}</b>\n{weeks} أسابيع، {days} يوم تدريبي.", "en": "✅ Programme saved: <b>{title}</b>\n{weeks} weeks, {days} training days."},
    "plan.today": {"ar": "📅 <b>تمرين اليوم:</b> {title}\n{exercises}", "en": "📅 <b>Today's session:</b> {title}\n{exercises}"},
    "plan.exercise_line": {"ar": "{i}. <b>{name}</b> — {sets}×{reps} (راحة {rest}ث)\n   {cue}\n   🎥 {video}", "en": "{i}. <b>{name}</b> — {sets}×{reps} (rest {rest}s)\n   {cue}\n   🎥 {video}"},
    "plan.rest_day": {"ar": "😴 اليوم راحة/استشفاء نشط: {note}", "en": "😴 Rest / active recovery today: {note}"},
    "plan.mark_done": {"ar": "✅ خلصت التمرين", "en": "✅ Workout done"},
    "plan.workout_done": {"ar": "💪 أحسنت! سُجّل التمرين ({minutes} دقيقة، {calories} سعرة). الالتزام: {streak} يوم.", "en": "💪 Well done! Session logged ({minutes} min, {calories} kcal). Streak: {streak} days."},
    "plan.safety": {"ar": "⚠️ <b>ملاحظات سلامة لك:</b>\n{notes}", "en": "⚠️ <b>Your safety notes:</b>\n{notes}"},

    # ── streak / badges ──────────────────────────────────────────────────────
    "streak.up": {"ar": "🔥 <b>{days} يوم التزام متتالي!</b> استمر.", "en": "🔥 <b>{days}-day streak!</b> Keep going."},
    "streak.badge": {"ar": "🏅 شارة جديدة: <b>{badge}</b>", "en": "🏅 New badge: <b>{badge}</b>"},
    "streak.broken": {"ar": "انقطع التزامك أمس. مو مشكلة — المشكلة تستمر. ارجع اليوم.", "en": "Your streak broke yesterday. Not a problem — staying broken is. Get back today."},

    # ── admin ────────────────────────────────────────────────────────────────
    "adm.title": {"ar": "<b>🛡️ لوحة الأدمن</b>", "en": "<b>🛡️ Admin panel</b>"},
    "adm.stats": {"ar": "📊 الإحصائيات", "en": "📊 Statistics"},
    "adm.users": {"ar": "👥 المستخدمون", "en": "👥 Users"},
    "adm.broadcast": {"ar": "📢 رسالة جماعية", "en": "📢 Broadcast"},
    "adm.ai": {"ar": "🤖 استهلاك API والتكلفة", "en": "🤖 API usage & cost"},
    "adm.channels": {"ar": "🔗 الاشتراك الإجباري", "en": "🔗 Forced subscription"},
    "adm.config": {"ar": "🧩 النماذج والأسعار والحدود", "en": "🧩 Models, pricing & caps"},
    "adm.audit": {"ar": "🧾 سجل الإجراءات", "en": "🧾 Audit log"},
    "adm.denied": {"ar": "⛔ هذه الأوامر للإدارة فقط.", "en": "⛔ Admins only."},
    "adm.search_ask": {"ar": "أرسل معرّف المستخدم (رقم Telegram) أو اسم المستخدم للبحث.", "en": "Send the Telegram ID or username to search."},
    "adm.user_card": {
        "ar": (
            "<b>👤 {name}</b>\n<code>{tg_id}</code>\n\n"
            "• النقاط: <b>{points}</b> (مكتسب {earned} / مصروف {spent})\n"
            "• الهدف: {goal} | المعدات: {equipment}\n"
            "• العمر {age} | الطول {height} سم | الوزن {weight} كغ\n"
            "• السعرات: {calories} (بروتين {protein} جم)\n"
            "• الالتزام: {streak} يوم (أفضل {best}) | التمارين: {workouts}\n"
            "• الكوتش: {coach} | الاشتراك: {sub}\n"
            "• آخر نشاط: {active}\n• الحالة: {status}"
        ),
        "en": (
            "<b>👤 {name}</b>\n<code>{tg_id}</code>\n\n• Points: <b>{points}</b> (earned {earned} / spent {spent})\n"
            "• Goal: {goal} | Equipment: {equipment}\n• Age {age} | Height {height} cm | Weight {weight} kg\n"
            "• Calories: {calories} (protein {protein} g)\n• Streak: {streak} (best {best}) | Workouts: {workouts}\n"
            "• Coach: {coach} | Subscription: {sub}\n• Last active: {active}\n• Status: {status}"
        ),
    },
    "adm.ban": {"ar": "🚫 حظر", "en": "🚫 Ban"},
    "adm.unban": {"ar": "✅ فك الحظر", "en": "✅ Unban"},
    "adm.add_points": {"ar": "➕ إضافة نقاط", "en": "➕ Add points"},
    "adm.sub_points": {"ar": "➖ خصم نقاط", "en": "➖ Deduct points"},
    "adm.view_profile": {"ar": "📂 الملف الكامل", "en": "📂 Full profile"},
    "adm.amount_ask": {"ar": "اكتب عدد النقاط (رقم).", "en": "Type the number of points."},
    "adm.points_done": {"ar": "✅ تم. رصيد المستخدم الآن: {balance}", "en": "✅ Done. New balance: {balance}"},
    "adm.ban_ask": {"ar": "اكتب سبب الحظر (أو «-» بدون سبب).", "en": "Type the ban reason (or “-” for none)."},
    "adm.banned": {"ar": "🚫 تم حظر المستخدم.", "en": "🚫 User banned."},
    "adm.unbanned": {"ar": "✅ تم فك الحظر.", "en": "✅ User unbanned."},
    "adm.stats_body": {
        "ar": (
            "<b>📊 إحصائيات البوت</b>\n\n"
            "• المستخدمون الكلي: <b>{total}</b>\n"
            "• نشط اليوم: <b>{today}</b> | هذا الأسبوع: <b>{week}</b> | 30 يوم: <b>{month}</b>\n"
            "• مكملو التسجيل: {onboarded} | مشتركو الكوتش: {coaches}\n"
            "• المحظورون: {banned}\n\n"
            "<b>🪙 الاقتصاد</b>\n{economy}\n\n<b>🤖 التكلفة (7 أيام)</b>\n{cost}"
        ),
        "en": (
            "<b>📊 Bot statistics</b>\n\n• Total users: <b>{total}</b>\n"
            "• Active today: <b>{today}</b> | this week: <b>{week}</b> | 30d: <b>{month}</b>\n"
            "• Onboarded: {onboarded} | Coach subscribers: {coaches}\n• Banned: {banned}\n\n"
            "<b>🪙 Economy</b>\n{economy}\n\n<b>🤖 Cost (7 days)</b>\n{cost}"
        ),
    },
    "adm.bc_ask": {
        "ar": "أرسل نص الرسالة الجماعية.\n(يمكنك إرفاق صورة/فيديو في نفس الرسالة)\n\nالجمهور: {audience}",
        "en": "Send the broadcast text.\n(you may attach a photo/video in the same message)\n\nAudience: {audience}",
    },
    "adm.bc_started": {"ar": "📢 بدأ الإرسال إلى {total} مستخدم…", "en": "📢 Sending to {total} users…"},
    "adm.bc_done": {"ar": "✅ اكتمل: أُرسل {sent}، فشل {failed} من {total}.", "en": "✅ Done: {sent} sent, {failed} failed of {total}."},
    "adm.ai_body": {
        "ar": (
            "<b>🤖 استهلاك API — آخر {days} أيام</b>\n\n"
            "• التكلفة الإجمالية: <b>${cost}</b>\n"
            "• عدد الاستدعاءات: {calls} | الأخطاء: {errors}\n"
            "• تكلفة اليوم: ${today}\n\n<b>حسب المهمة:</b>\n{by_task}\n\n<b>حسب الموديل:</b>\n{by_model}"
        ),
        "en": (
            "<b>🤖 API usage — last {days} days</b>\n\n• Total cost: <b>${cost}</b>\n"
            "• Calls: {calls} | Errors: {errors}\n• Today: ${today}\n\n<b>By task</b>\n{by_task}\n\n<b>By model</b>\n{by_model}"
        ),
    },
    "adm.config_body": {
        "ar": "<b>🧩 الإعدادات الحية</b>\n\n<b>توزيع الموديلات:</b>\n{routing}\n\n<b>الحدود:</b>\n{caps}",
        "en": "<b>🧩 Live settings</b>\n\n<b>Model routing</b>\n{routing}\n\n<b>Caps</b>\n{caps}",
    },
    "adm.channels_body": {
        "ar": "<b>🔗 الاشتراك الإجباري</b>\nالحالة: {state}\nالقنوات: {channels}",
        "en": "<b>🔗 Forced subscription</b>\nState: {state}\nChannels: {channels}",
    },
    "adm.audit_body": {"ar": "<b>🧾 آخر الإجراءات</b>\n{rows}", "en": "<b>🧾 Recent actions</b>\n{rows}"},

    # ── banned ───────────────────────────────────────────────────────────────
    "banned.body": {
        "ar": "🚫 تم إيقاف حسابك من الإدارة.{reason}\nللاستفسار تواصل مع: {contact}",
        "en": "🚫 Your account was suspended by the admins.{reason}\nContact: {contact}",
    },

    # ── misc ─────────────────────────────────────────────────────────────────
    "lang.changed": {"ar": "✅ تم تغيير اللغة إلى العربية.", "en": "✅ Language switched to English."},
    "voice.transcribed": {"ar": "🎙️ <b>رسالتك الصوتية:</b>\n«{text}»", "en": "🎙️ <b>Your voice note:</b>\n“{text}”"},
    "voice.failed": {"ar": "ما قدرت أحوّل الصوت لنص. اكتب رسالتك نصًا من فضلك.", "en": "Couldn't transcribe that. Please type your message."},
    "throttle": {"ar": "⏳ على مهلك — أرسل رسالة واحدة كل {seconds} ثانية.", "en": "⏳ Slow down — one message every {seconds}s."},
    "budget.hit": {
        "ar": "🛑 وصلنا سقف استهلاك الذكاء الاصطناعي اليومي للخدمة. الإدارة على علم — جرّب بعد قليل.",
        "en": "🛑 The service hit its daily AI budget. Admins are aware — try again shortly.",
    },
}


def t(lang: str | Language | None, key: str, **kwargs: Any) -> str:
    """Translate ``key`` and format it with ``kwargs``."""
    entry = STRINGS.get(key)
    if entry is None:
        return key
    code = "en" if str(lang or "ar").startswith("en") else "ar"
    text = entry.get(code) or entry.get("ar") or entry.get("en") or key
    if kwargs:
        try:
            return text.format(**kwargs)
        except (KeyError, IndexError):
            return text
    return text


def is_ar(lang: str | Language | None) -> bool:
    return not str(lang or "ar").startswith("en")


def detect_language(code: str | None) -> str:
    if not code:
        return Language.AR.value
    return Language.EN.value if code.lower().startswith("en") else Language.AR.value


def localize_number(value: float | int, lang: str = "ar") -> str:
    """Western digits are the convention in technical Arabic UIs — keep them."""
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


_PLACEHOLDER = re.compile(r"\{(\w+)\}")


def missing_keys(lang: str = "ar") -> list[str]:
    """Dev helper: keys lacking a translation for ``lang``."""
    return [k for k, v in STRINGS.items() if not v.get(lang)]
