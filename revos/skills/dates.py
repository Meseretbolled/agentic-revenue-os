"""Reading a return date out of an out-of-office reply. Plain rules; a miss returns None."""

import re
from datetime import date, timedelta

from revos.domain.untrusted import UntrustedText

_MONTHS = {m: i for i, m in enumerate(
    "january february march april may june july august september october november december".split(),
    start=1)}
_MONTH = "|".join(sorted(_MONTHS, key=len, reverse=True)) + "|" + "|".join(
    m[:3] for m in _MONTHS) + "|sept"
_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]

ISO = r"\b\d{4}-\d{2}-\d{2}\b"
DAY_MONTH = rf"\b\d{{1,2}}(?:st|nd|rd|th)?\s+(?:of\s+)?(?:{_MONTH})\b\.?(?:,?\s+\d{{4}})?"
MONTH_DAY = rf"\b(?:{_MONTH})\b\.?\s+\d{{1,2}}(?:st|nd|rd|th)?\b(?:,?\s+\d{{4}})?"
WEEKDAY = rf"\b(?:until|till|back on|return(?:ing|s)? on|back)\s+(?:{'|'.join(_WEEKDAYS)})\b"
RELATIVE = r"\b(?:next week|in \d{1,2} (?:days?|weeks?))\b"
PATTERN = "|".join(f"(?:{p})" for p in (ISO, DAY_MONTH, MONTH_DAY, WEEKDAY, RELATIVE))


def _month(word: str) -> int:
    word = word.lower().rstrip(".")
    return next(n for m, n in _MONTHS.items() if m == word or m[:3] == word[:3])


def _year_for(month: int, day: int, year: int | None, today: date) -> date | None:
    try:
        if year is not None:
            return date(year, month, day)
        candidate = date(today.year, month, day)
        return candidate if candidate >= today else date(today.year + 1, month, day)
    except ValueError:
        return None


def _parse(snippet: str, today: date) -> date | None:
    s = snippet.lower()
    if m := re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", s):
        try:
            return date(int(m[1]), int(m[2]), int(m[3]))
        except ValueError:
            return None
    if s.startswith("next week"):
        return today + timedelta(days=7)
    if m := re.fullmatch(r"in (\d+) (day|week)s?", s):
        return today + timedelta(days=int(m[1]) * (7 if m[2] == "week" else 1))
    for i, name in enumerate(_WEEKDAYS):
        if name in s and not re.search(rf"\b(?:{_MONTH})\b", s):
            return today + timedelta(days=(i - today.weekday() - 1) % 7 + 1)
    if m := re.match(rf"(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?({_MONTH})\b\.?(?:,?\s+(\d{{4}}))?", s):
        return _year_for(_month(m[2]), int(m[1]), int(m[3]) if m[3] else None, today)
    if m := re.match(rf"({_MONTH})\b\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?\b(?:,?\s+(\d{{4}}))?", s):
        return _year_for(_month(m[1]), int(m[2]), int(m[3]) if m[3] else None, today)
    return None


def return_date(reply: UntrustedText, today: date) -> date | None:
    """The first date the reply states for coming back, or None. Never in the past."""
    for snippet in reply.snippets(PATTERN):
        parsed = _parse(snippet, today)
        if parsed and parsed > today:
            return parsed
    return None
