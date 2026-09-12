"""Input parsing/validation helpers for the onboarding FSM and quick logs."""
from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any

AR_DIGITS = str.maketrans(
    "٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹٫٬",     # Arabic-Indic + Persian digits, decimal & thousands separators
    "01234567890123456789.,",
)
NUMBER_RE = re.compile(r"-?\d+(?:[.,]\d+)?")
SKIP_WORDS = {
    "تخطي", "تجاوز", "skip", "لاحقا", "لاحقًا", "بعدين", "لا شيء", "لاشيء", "مافي", "ماكو", "none", "-",
    "later", "ماشي",
}
NEGATIVE_WORDS = {
    "لا", "لا شيء", "لاشيء", "ما عندي", "مافي", "ماكو", "لا يوجد", "none", "no", "nothing", "n/a", "-", "0",
    "سليم", "—", "ماشي", "مافي اشي", "ما في", "ولا شي", "ولا شيء",
}

# spoken Arabic/English quantities — "كاستين ماء" carries no digits at all
WORD_NUMBERS: dict[str, int] = {
    "واحد": 1, "واحده": 1, "واحدة": 1, "one": 1,
    "اثنين": 2, "اثنتين": 2, "تنين": 2, "two": 2,
    "ثلاث": 3, "ثلاثة": 3, "three": 3,
    "اربع": 4, "أربع": 4, "اربعة": 4, "أربعة": 4, "four": 4,
    "خمس": 5, "خمسة": 5, "five": 5,
    "ست": 6, "ستة": 6, "six": 6,
    "سبع": 7, "سبعة": 7, "seven": 7,
    "ثمن": 8, "ثمانية": 8, "eight": 8,
}
DUAL_SUFFIX = "ين"            # "كاستين" / "كوبين" = two of them
DUAL_WORDS = {"كاستين", "كاسين", "كأسين", "كوبين", "قنينتين", "زجاجتين"}   # the Arabic dual = exactly two


def normalize_digits(text: str | None) -> str:
    return (text or "").translate(AR_DIGITS).strip()


def parse_float(text: str | None, *, min_value: float | None = None, max_value: float | None = None) -> float | None:
    """First number in the message, Arabic digits included."""
    cleaned = normalize_digits(text)
    match = NUMBER_RE.search(cleaned.replace("٬", "").replace("،", ","))
    if not match:
        return None
    try:
        value = float(match.group(0).replace(",", "."))
    except ValueError:
        return None
    if min_value is not None and value < min_value:
        return None
    if max_value is not None and value > max_value:
        return None
    return value


def parse_int(text: str | None, *, min_value: int | None = None, max_value: int | None = None) -> int | None:
    value = parse_float(text, min_value=min_value, max_value=max_value)
    return int(value) if value is not None else None


def is_skip(text: str | None) -> bool:
    return normalize_digits(text).lower().strip(" .!،?؟") in SKIP_WORDS


def is_negative(text: str | None) -> bool:
    cleaned = normalize_digits(text).lower().strip(" .!،?؟")
    if not cleaned:
        return True
    return cleaned in NEGATIVE_WORDS or (len(cleaned) < 4 and cleaned in {"لا", "no"})


def parse_weight(text: str | None) -> float | None:
    return parse_float(text, min_value=25, max_value=400)


def parse_height(text: str | None) -> float | None:
    """``178``, ``178 سم``, ``1.78``, ``1.78 m`` → centimetres."""
    value = parse_float(text, min_value=0.5, max_value=300)
    if value is None:
        return None
    if value < 3:                                  # given in metres
        value *= 100
    if not 90 <= value <= 260:
        return None
    return round(value, 1)


def parse_age(text: str | None) -> int | None:
    return parse_int(text, min_value=10, max_value=100)


def parse_measurements(text: str | None) -> dict[str, float]:
    """Find every ``label: number`` pair in free text.

    Works for one field per line (``خصر: 88``), several on a line
    (``الخصر 92 الصدر 104``) and English (``waist 90cm, hip 98cm``).
    """
    if not text:
        return {}
    aliases: dict[str, list[str]] = {
        "waist_cm": ["خصر", "الخصر", "وسط", "waist", "belly"],
        "chest_cm": ["صدر", "الصدر", "chest"],
        "arm_cm": ["ذراع", "الذراع", "عضد", "باي", "arm", "bicep"],
        "thigh_cm": ["فخذ", "الفخذ", "thigh"],
        "hip_cm": ["ورك", "الورك", "أرداف", "hip", "hips"],
        "neck_cm": ["رقبة", "الرقبة", "neck"],
        "calf_cm": ["ربلة", "سمانة", "calf"],
        "shoulder_cm": ["كتف", "أكتاف", "shoulder", "shoulders"],
        "body_fat_pct": ["نسبة الدهون", "دهون", "body fat", "bodyfat", "fat", "bf"],
    }
    cleaned = normalize_digits(text)
    out: dict[str, float] = {}
    for field, words in aliases.items():
        for word in sorted(words, key=len, reverse=True):
            # label, then up to a few separators, then the number
            match = re.search(
                rf"{re.escape(word)}\s*(?:is|:|=|→|-)?\s*(-?\d+(?:[.,]\d+)?)", cleaned, re.IGNORECASE
            )
            if not match:
                continue
            value = float(match.group(1).replace(",", "."))
            if field == "body_fat_pct" and not 3 <= value <= 70:
                continue
            if field != "body_fat_pct" and not 15 <= value <= 250:
                continue
            out[field] = value
            break
    return out


WATER_WORDS = ("ماء", "ماي", "مياه", "water", "كاس", "كأس", "كوب", "glass", "cup", "قنينة", "زجاجة",
               "bottle", "لتر", "liter", "litre", "مل", "ml")
CONTAINER_ML = (("لتر", 1000), ("liter", 1000), ("litre", 1000), ("قنينة", 500), ("زجاجة", 500),
                ("bottle", 500), ("كاس", 250), ("كأس", 250), ("كوب", 250), ("glass", 250), ("cup", 250))


def _spoken_number(text: str) -> float | None:
    """``كاستين`` → 2, ``ثلاث أكواب`` → 3 (Arabic speakers rarely type digits here)."""
    import re as _re

    lowered = text.lower()
    if any(word in lowered for word in DUAL_WORDS):     # "كاستين" = two glasses
        return 2.0
    # longest word first, and only as a whole word ("ست" must not match inside "كاستين")
    for word in sorted(WORD_NUMBERS, key=len, reverse=True):
        if _re.search(rf"(?<!\w){_re.escape(word)}(?!\w)", lowered):
            return float(WORD_NUMBERS[word])
    if any((stem + DUAL_SUFFIX) in lowered for stem in ("كاس", "كوب", "قنين", "زجاج")):
        return 2.0
    return None


def parse_water(text: str | None) -> int | None:
    """``كاستين ماء``, ``500 مل``, ``1 لتر``, ``2 glasses`` → millilitres.

    Returns ``None`` unless the message actually mentions water, so a number in
    an unrelated sentence ("burn 300 calories") is never logged as water.
    """
    if not text:
        return None
    cleaned = normalize_digits(text).lower()
    has_ml_unit = "مل" in cleaned or re.search(r"\bml\b", cleaned)
    if not has_ml_unit and not any(word in cleaned for word in WATER_WORDS):
        return None

    digits = parse_float(cleaned, min_value=0.05, max_value=20000)
    if digits is None and not re.search(r"\d", cleaned):
        digits = _spoken_number(cleaned)          # "لتر ماء" / "كاستين ماء"
    if digits is None:
        digits = 1.0 if has_ml_unit else _spoken_number(cleaned) or 1.0
    if digits is None:
        return None

    if has_ml_unit:
        return int(round(digits))
    if re.search(r"\bl\b", cleaned) or "لتر" in cleaned or "liter" in cleaned or "litre" in cleaned:
        return int(round(digits * 1000)) if digits <= 5 else int(round(digits))
    for word, millilitres in CONTAINER_ML:
        if word in cleaned:
            return int(round(digits * millilitres))
    return int(round(digits * 250)) if digits <= 20 else int(round(digits))


def parse_time_of_day(text: str | None) -> tuple[int, int] | None:
    cleaned = normalize_digits(text or "")
    match = re.search(r"(\d{1,2})\s*[:.]?\s*(\d{2})?\s*(ص|م|am|pm)?", cleaned, re.IGNORECASE)
    if not match:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    suffix = (match.group(3) or "").lower()
    if suffix in ("م", "pm") and hour < 12:
        hour += 12
    if suffix in ("ص", "am") and hour == 12:
        hour = 0
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour, minute


TZ_ALIASES: dict[str, str] = {
    "سوريا": "Asia/Damascus", "syria": "Asia/Damascus", "دمشق": "Asia/Damascus",
    "السعودية": "Asia/Riyadh", "saudi": "Asia/Riyadh", "الرياض": "Asia/Riyadh", "riyadh": "Asia/Riyadh",
    "مصر": "Africa/Cairo", "egypt": "Africa/Cairo", "القاهرة": "Africa/Cairo", "cairo": "Africa/Cairo",
    "الامارات": "Asia/Dubai", "الإمارات": "Asia/Dubai", "دبي": "Asia/Dubai", "uae": "Asia/Dubai",
    "الكويت": "Asia/Kuwait", "kuwait": "Asia/Kuwait",
    "قطر": "Asia/Qatar", "qatar": "Asia/Qatar", "دوحة": "Asia/Qatar",
    "العراق": "Asia/Baghdad", "iraq": "Asia/Baghdad", "بغداد": "Asia/Baghdad",
    "الاردن": "Asia/Amman", "الأردن": "Asia/Amman", "jordan": "Asia/Amman",
    "لبنان": "Asia/Beirut", "lebanon": "Asia/Beirut", "بيروت": "Asia/Beirut",
    "فلسطين": "Asia/Jerusalem", "palestine": "Asia/Jerusalem",
    "المغرب": "Africa/Casablanca", "morocco": "Africa/Casablanca",
    "الجزائر": "Africa/Algiers", "algeria": "Africa/Algiers",
    "تونس": "Africa/Tunis", "tunisia": "Africa/Tunis",
    "ليبيا": "Africa/Tripoli", "libya": "Africa/Tripoli",
    "تركيا": "Europe/Istanbul", "turkey": "Europe/Istanbul", "istanbul": "Europe/Istanbul",
    "المانيا": "Europe/Berlin", "ألمانيا": "Europe/Berlin", "germany": "Europe/Berlin",
    "فرنسا": "Europe/Paris", "france": "Europe/Paris",
    "هولندا": "Europe/Amsterdam", "netherlands": "Europe/Amsterdam",
    "السويد": "Europe/Stockholm", "sweden": "Europe/Stockholm",
    "بريطانيا": "Europe/London", "uk": "Europe/London", "london": "Europe/London",
    "كندا": "America/Toronto", "canada": "America/Toronto", "toronto": "America/Toronto",
    "مونتريال": "America/Toronto", "montreal": "America/Toronto",
    "امريكا": "America/New_York", "أمريكا": "America/New_York", "usa": "America/New_York",
    "gmt": "UTC", "utc": "UTC",
}


def parse_timezone(text: str | None) -> str | None:
    """``Asia/Damascus`` (any casing) or a spoken country/city name → IANA zone."""
    cleaned = (text or "").strip()
    if not cleaned:
        return None

    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    # IANA names are case-sensitive; forgive "asia/damascus" and "Asia/damascus"
    for candidate in (cleaned, cleaned.title(), cleaned.capitalize(), cleaned.upper()):
        try:
            ZoneInfo(candidate)
        except (ZoneInfoNotFoundError, ValueError, KeyError):
            continue
        except Exception:  # noqa: BLE001 — tzdata missing → fall back to aliases
            break
        return candidate

    key = cleaned.lower()
    for alias, zone in TZ_ALIASES.items():
        if key == alias.lower() or alias in cleaned:
            return zone
    return None


def parse_date(text: str | None) -> date | None:
    cleaned = normalize_digits(text or "").strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%m/%d/%Y", "%d.%m.%Y"):
        try:
            return datetime.strptime(cleaned, fmt).date()
        except ValueError:
            continue
    return None


def truncate(text: str, limit: int = 4000) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def safe_str(value: Any, limit: int = 200) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text[:limit] or None
