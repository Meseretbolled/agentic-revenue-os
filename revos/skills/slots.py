"""Matching a prospect's acceptance to one of the times we offered."""

import re
from collections.abc import Sequence
from datetime import datetime

from revos.domain.untrusted import UntrustedText

_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
_MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august", "september",
           "october", "november", "december"]
_ORDINALS = {"first": 0, "1st": 0, "second": 1, "2nd": 1, "third": 2, "3rd": 2}

_WEEKDAY = rf"\b(?:{'|'.join(_WEEKDAYS)})s?\b"
_DAY_MONTH = rf"\b\d{{1,2}}(?:st|nd|rd|th)?\s+(?:of\s+)?(?:{'|'.join(_MONTHS)})\b"
_MONTH_DAY = rf"\b(?:{'|'.join(_MONTHS)})\s+\d{{1,2}}(?:st|nd|rd|th)?\b"
_CLOCK = r"\b\d{1,2}:\d{2}\b"
_AT_HOUR = r"\bat\s+\d{1,2}\s?(?:am|pm)?(?!\d|:)"
_ORDINAL = (r"\b(?:the\s+)?(?:first|second|third|1st|2nd|3rd)(?:\s+(?:one|option|slot|time))?\b"
            r"|\boption\s*[123]\b")
PATTERN = "|".join(
    f"(?:{p})" for p in (_WEEKDAY, _DAY_MONTH, _MONTH_DAY, _CLOCK, _AT_HOUR, _ORDINAL))


def slot_text(start: datetime) -> str:
    """How a slot is written in our emails, so a reply can be checked against what we offered."""
    return f"{start:%A %d %B %H:%M} UTC"


def _time(snippet: str) -> tuple[int, int | None]:
    """(hour, minute). A bare 'at 11' leaves the minute open; a clock time '11:30' fixes it."""
    m = re.search(r"(\d{1,2})(?::(\d{2}))?\s?(am|pm)?", snippet)
    hour = int(m.group(1))
    if m.group(3) == "pm" and hour < 12:
        hour += 12
    if m.group(3) == "am" and hour == 12:
        hour = 0
    return hour, int(m.group(2)) if m.group(2) else None


def match_slot(reply: UntrustedText, offered: Sequence[datetime]) -> datetime | None:
    """The one offered slot the reply names, or None if it names none or more than one.

    Only snippets that look like a weekday, a date, a clock time or "the second one" are read."""
    days: set[int] = set()
    weekdays: set[int] = set()
    times: set[tuple[int, int | None]] = set()
    ordinal: int | None = None
    for snippet in reply.snippets(PATTERN):
        low = snippet.lower()
        if any(name in low for name in _WEEKDAYS):
            weekdays.add(next(i for i, n in enumerate(_WEEKDAYS) if n in low))
        elif any(month in low for month in _MONTHS):
            days.add(int(re.search(r"\d{1,2}", low).group(0)))
        elif re.search(r"\d:\d\d", low) or low.startswith("at"):
            times.add(_time(low))
        else:
            word = re.search(r"first|second|third|1st|2nd|3rd|[123]", low).group(0)
            ordinal = _ORDINALS.get(word, int(word) - 1 if word.isdigit() else None)
    if ordinal is not None:
        return offered[ordinal] if 0 <= ordinal < len(offered) else None
    if not (days or weekdays or times):
        return None
    fits = [s for s in offered
            if (not weekdays or s.weekday() in weekdays) and (not days or s.day in days)
            and (not times or any(s.hour == h and (m is None or s.minute == m) for h, m in times))]
    return fits[0] if len(fits) == 1 else None
