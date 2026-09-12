"""Date helpers shared by jobs, reports and handlers.

Everything is timezone-aware: a user's "today" is their own local calendar day
(spec §5.4 — daily tracking must reset at the user's midnight, not UTC's).
"""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

DEFAULT_TZ = "UTC"


def zone_of(tz: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(tz or DEFAULT_TZ)
    except Exception:  # noqa: BLE001 — unknown zone name → fall back to UTC
        return ZoneInfo(DEFAULT_TZ)


def utcnow() -> datetime:
    return datetime.now(UTC)


def local_now(tz: str | None = None) -> datetime:
    return datetime.now(zone_of(tz))


def local_today(tz: str | None = None) -> date:
    return local_now(tz).date()


def user_now(user: Any) -> datetime:
    return local_now(getattr(user, "user_timezone", None))


def user_today(user: Any) -> date:
    return user_now(user).date()


def week_bounds(day: date | None = None, *, start_monday: bool = True) -> tuple[date, date]:
    """Inclusive (start, end) of the week containing ``day``."""
    day = day or local_today()
    start = day - timedelta(days=day.weekday() if start_monday else (day.weekday() + 1) % 7)
    return start, start + timedelta(days=6)


def month_bounds(day: date | None = None) -> tuple[date, date]:
    day = day or local_today()
    start = day.replace(day=1)
    if start.month == 12:
        end = start.replace(year=start.year + 1, month=1) - timedelta(days=1)
    else:
        end = start.replace(month=start.month + 1) - timedelta(days=1)
    return start, end


def days_between(start: date, end: date | None = None) -> int:
    return (end or local_today()).toordinal() - start.toordinal()


def date_range(start: date, end: date) -> list[date]:
    """Inclusive list of dates (used for streak calendars and charts)."""
    if end < start:
        return []
    return [start + timedelta(days=offset) for offset in range((end - start).days + 1)]


def format_dt(value: datetime | date | None, lang: str = "ar", *, with_time: bool = True) -> str:
    if value is None:
        return "—"
    if isinstance(value, datetime):
        pattern = "%Y-%m-%d %H:%M" if with_time else "%Y-%m-%d"
    else:
        pattern = "%Y-%m-%d"
    stamp = value.strftime(pattern)
    if not lang.startswith("ar"):
        return stamp
    months = {
        "01": "يناير", "02": "فبراير", "03": "مارس", "04": "أبريل", "05": "مايو", "06": "يونيو",
        "07": "يوليو", "08": "أغسطس", "09": "سبتمبر", "10": "أكتوبر", "11": "نوفمبر", "12": "ديسمبر",
    }
    parts = stamp.split(" ")
    date_part = parts[0].split("-")
    if len(date_part) == 3:
        arabic = f"{int(date_part[2])} {months.get(date_part[1], date_part[1])} {date_part[0]}"
        return arabic + (f" {parts[1]}" if len(parts) > 1 else "")
    return stamp


def human_delta(days: int, lang: str = "ar") -> str:
    ar = lang.startswith("ar")
    if days == 0:
        return "اليوم" if ar else "today"
    if days == 1:
        return "غدًا" if ar else "tomorrow"
    if 2 <= days <= 10:
        return f"بعد {days} أيام" if ar else f"in {days} days"
    return f"بعد {days} يوم" if ar else f"in {days} days"


def in_quiet_hours(user: Any, when: datetime | None = None, start: int = 23, end: int = 7) -> bool:
    """True when local time is inside the do-not-disturb window."""
    moment = (when.astimezone(zone_of(getattr(user, "user_timezone", None))) if when
              else user_now(user))
    hour = moment.hour
    if start == end:
        return False
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


__all__ = [
    "DEFAULT_TZ",
    "zone_of",
    "utcnow",
    "local_now",
    "local_today",
    "user_now",
    "user_today",
    "week_bounds",
    "month_bounds",
    "days_between",
    "date_range",
    "format_dt",
    "human_delta",
    "in_quiet_hours",
]
