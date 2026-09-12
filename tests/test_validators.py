"""Input parsing: Arabic-Indic digits, units, measurements, times, timezones."""
from __future__ import annotations

import pytest

from app.utils import validators as v


def test_normalize_digits_converts_arabic_indic() -> None:
    assert v.normalize_digits("٨١٫٤") == "81.4"
    assert v.normalize_digits("۸۱٫۴") == "81.4"       # Eastern Arabic (Persian) too
    assert v.normalize_digits("81.4") == "81.4"
    assert v.normalize_digits(None) == ""


@pytest.mark.parametrize("raw,expected", [
    ("81.4", 81.4), ("٨١٫٤", 81.4), ("81,4", 81.4), ("وزني 81.4 كجم", 81.4),
    ("81 kg", 81.0), ("81 كيلو", 81.0),
])
def test_parse_weight_accepts_real_world_formats(raw: str, expected: float) -> None:
    assert v.parse_weight(raw) == pytest.approx(expected, abs=0.05)


@pytest.mark.parametrize("raw", ["", "abc", "-5", "0", "900", None, "١٢٠٠"])
def test_parse_weight_rejects_nonsense(raw) -> None:
    assert v.parse_weight(raw) is None


def test_parse_height_handles_meters_and_cm() -> None:
    assert v.parse_height("178") == pytest.approx(178.0)
    assert v.parse_height("1.78") == pytest.approx(178.0)
    assert v.parse_height("١٧٨ سم") == pytest.approx(178.0)
    assert v.parse_height("300") is None
    assert v.parse_height("نص") is None


def test_parse_age_bounds() -> None:
    assert v.parse_age("28") == 28
    assert v.parse_age("٢٨ سنة") == 28
    assert v.parse_age("5") is None
    assert v.parse_age("150") is None


def test_parse_measurements_finds_several_fields() -> None:
    values = v.parse_measurements("الخصر 92 الصدر 104 الذراع 34")
    assert values["waist_cm"] == pytest.approx(92)
    assert values["chest_cm"] == pytest.approx(104)
    assert values["arm_cm"] == pytest.approx(34)


def test_parse_measurements_english_and_empty() -> None:
    parsed = v.parse_measurements("waist 90cm, hip 98cm")
    assert parsed["waist_cm"] == pytest.approx(90)
    assert parsed["hip_cm"] == pytest.approx(98)
    assert v.parse_measurements("خصر: 88")["waist_cm"] == pytest.approx(88)
    assert v.parse_measurements("body fat 22%")["body_fat_pct"] == pytest.approx(22)
    assert v.parse_measurements("ما شي") == {}
    assert v.parse_measurements(None) == {}


@pytest.mark.parametrize("raw,expected", [
    ("كاستين ماء", 500), ("كوب ماء", 250), ("3 glasses of water", 750),
    ("500 مل", 500), ("1 لتر", 1000), ("لتر ماء", 1000),
    ("ثلاث أكواب ماء", 750), ("قنينة ماء", 500), ("one bottle of water", 500),
])
def test_parse_water(raw: str, expected: int) -> None:
    assert v.parse_water(raw) == expected


def test_parse_water_rejects_other_numbers() -> None:
    assert v.parse_water("وزني 81") is None


def test_parse_time_of_day() -> None:
    assert v.parse_time_of_day("14:30") == (14, 30)
    assert v.parse_time_of_day("٢:٠٥ م") == (14, 5)
    assert v.parse_time_of_day("9") == (9, 0)
    assert v.parse_time_of_day("25:00") is None
    assert v.parse_time_of_day("مساء") is None


def test_parse_timezone_validates_the_zone() -> None:
    assert v.parse_timezone("Asia/Damascus") == "Asia/Damascus"
    assert v.parse_timezone("asia/damascus") == "Asia/Damascus"   # case-tolerant
    assert v.parse_timezone("سوريا") == "Asia/Damascus"           # spoken country name
    assert v.parse_timezone("مصر") == "Africa/Cairo"
    assert v.parse_timezone("Mars/Olympus") is None
    assert v.parse_timezone("") is None


def test_parse_date_and_int_and_float() -> None:
    assert v.parse_date("2026-09-03") is not None
    assert v.parse_date("nope") is None
    assert v.parse_int("٥٠٠") == 500
    assert v.parse_int("abc") is None
    assert v.parse_float("1.5", min_value=2.0) is None
    assert v.parse_float("1.5", max_value=1.0) is None


@pytest.mark.parametrize("raw", ["لا", "لا شيء", "none", "nothing", "ماشي", "لا يوجد", "no", "—", "-"])
def test_is_negative(raw: str) -> None:
    assert v.is_negative(raw) is True


@pytest.mark.parametrize("raw", ["تخطي", "skip", "بعدين", "لاحقًا", "later"])
def test_is_skip(raw: str) -> None:
    assert v.is_skip(raw) is True


def test_is_negative_and_skip_do_not_confuse_real_answers() -> None:
    assert v.is_negative("عندي ألم في الركبة") is False
    assert v.is_skip("ما أكلت الغدا") is False


def test_safe_str_truncates_and_strips() -> None:
    assert v.safe_str("  hello  ", 3) == "hel"
    assert v.safe_str(None) is None


def test_truncate_keeps_within_the_limit() -> None:
    from app.utils.text import truncate

    assert len(truncate("x" * 5000, 4000)) <= 4000
    assert truncate("short") == "short"
