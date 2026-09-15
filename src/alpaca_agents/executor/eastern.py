"""US/Eastern session dates without tzdata. Post-2007 DST rules only.

Prefer zoneinfo when the system has it; fall back to the statutory rule
(second Sunday of March 02:00 local -> first Sunday of November 02:00 local).
"""
from datetime import date, datetime, timedelta, timezone

try:  # pragma: no cover - depends on host tz database
    from zoneinfo import ZoneInfo
    _EASTERN = ZoneInfo("America/New_York")
except Exception:  # ZoneInfoNotFoundError or missing tzdata on Windows
    _EASTERN = None


def _nth_sunday(year: int, month: int, n: int) -> date:
    first = date(year, month, 1)
    offset = (6 - first.weekday()) % 7          # Monday=0 ... Sunday=6
    return first + timedelta(days=offset + 7 * (n - 1))


def eastern_offset(utc: datetime) -> timedelta:
    """Offset for an aware UTC instant using the statutory rule."""
    year = utc.year
    start = datetime.combine(_nth_sunday(year, 3, 2), datetime.min.time(), timezone.utc) + timedelta(hours=7)   # 02:00 EST = 07:00Z
    end = datetime.combine(_nth_sunday(year, 11, 1), datetime.min.time(), timezone.utc) + timedelta(hours=6)    # 02:00 EDT = 06:00Z
    return timedelta(hours=-4) if start <= utc < end else timedelta(hours=-5)


def to_eastern(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("aware datetime required")
    if value.year < 2007:
        raise ValueError("pre-2007 DST rules unsupported")
    if _EASTERN is not None:
        return value.astimezone(_EASTERN)
    utc = value.astimezone(timezone.utc)
    return (utc + eastern_offset(utc)).replace(tzinfo=timezone(eastern_offset(utc)))


def eastern_date(value: datetime) -> date:
    return to_eastern(value).date()
