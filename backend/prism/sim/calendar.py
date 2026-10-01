"""Business-day calendar (weekends only; no holiday calendar in the MVP)."""
from datetime import UTC, date, datetime, timedelta


def business_days(as_of: date, n: int) -> list[date]:
    """The n most recent weekdays ending at as_of (inclusive when as_of is a weekday), ascending."""
    days: list[date] = []
    d = as_of
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d)
        d -= timedelta(days=1)
    return list(reversed(days))


def at(d: date, hour: int, minute: int = 0) -> datetime:
    return datetime(d.year, d.month, d.day, hour, minute, tzinfo=UTC)
