"""Send windows in the prospect's own time zone."""

from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from revos.policy.config import SendWindow


def zone(name: str | None, fallback: str) -> ZoneInfo:
    for candidate in (name, fallback, "UTC"):
        if candidate:
            try:
                return ZoneInfo(candidate)
            except ZoneInfoNotFoundError:
                continue
    return ZoneInfo("UTC")


def next_open(at: datetime, tz: ZoneInfo, window: SendWindow) -> datetime:
    """`at` itself if the window is open then, otherwise the next moment it opens (UTC)."""
    local = at.astimezone(tz)
    for offset in range(9):
        day = local.date() + timedelta(days=offset)
        if day.weekday() not in window.days:
            continue
        start = datetime.combine(day, time(window.start_hour), tzinfo=tz)
        end = datetime.combine(day, time(window.end_hour), tzinfo=tz)
        if offset == 0 and start <= local < end:
            return at
        if start > local:
            return start.astimezone(UTC)
    raise ValueError("send window has no open day")
