"""NYSE trading-session calendar from the published holiday rules. Pure; no I/O.

Covers the nine regular holidays plus Juneteenth (observed since 2022) and the
weekend-observance rule (Saturday -> Friday, Sunday -> Monday), and Good Friday.
Ad-hoc closures (national days of mourning, weather, 9/11) are NOT modelled;
the broker clock's `is_open` remains the authority for "is the market open right
now". This calendar answers "was/is this date a scheduled session", which the
broker clock cannot answer for past dates.
"""
from datetime import date, timedelta


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + timedelta(days=offset + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    nxt = date(year + (month == 12), month % 12 + 1, 1)
    last = nxt - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def easter(year: int) -> date:
    """Anonymous Gregorian algorithm."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month, day = divmod(h + l - 7 * m + 114, 31)
    return date(year, month, day + 1)


def _observed(day: date) -> date | None:
    """Weekend holidays move to the adjacent weekday; a Saturday New Year's Day is not observed on Dec 31."""
    if day.weekday() == 5:
        return None if (day.month == 1 and day.day == 1) else day - timedelta(days=1)
    if day.weekday() == 6:
        return day + timedelta(days=1)
    return day


def holidays(year: int) -> frozenset:
    fixed = [date(year, 1, 1), date(year, 7, 4), date(year, 12, 25)]
    if year >= 2022:
        fixed.append(date(year, 6, 19))
    days = {_observed(d) for d in fixed} - {None}
    days |= {
        _nth_weekday(year, 1, 0, 3),      # MLK: third Monday of January
        _nth_weekday(year, 2, 0, 3),      # Presidents' Day: third Monday of February
        easter(year) - timedelta(days=2),  # Good Friday
        _last_weekday(year, 5, 0),        # Memorial Day: last Monday of May
        _nth_weekday(year, 9, 0, 1),      # Labor Day: first Monday of September
        _nth_weekday(year, 11, 3, 4),     # Thanksgiving: fourth Thursday of November
    }
    return frozenset(days)


def is_session(day: date) -> bool:
    if type(day) is not date:
        raise TypeError("date required")
    return day.weekday() < 5 and day not in holidays(day.year)


def previous_session(day: date) -> date:
    """Most recent scheduled session strictly before day."""
    d = day - timedelta(days=1)
    while not is_session(d):
        d -= timedelta(days=1)
    return d


def sessions_between(start: date, end: date) -> int:
    """Scheduled sessions in (start, end]."""
    if end <= start:
        return 0
    count, d = 0, start + timedelta(days=1)
    while d <= end:
        count += is_session(d)
        d += timedelta(days=1)
    return count
