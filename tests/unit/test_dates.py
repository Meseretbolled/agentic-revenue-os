from datetime import date

import pytest

from revos.domain.untrusted import UntrustedText
from revos.skills.dates import return_date

TODAY = date(2026, 10, 12)          # a Monday


@pytest.mark.parametrize("text,expected", [
    ("I am out of office until Monday 19 October.", date(2026, 10, 19)),
    ("Back on October 20th, thanks.", date(2026, 10, 20)),
    ("Away until 5 Nov", date(2026, 11, 5)),
    ("Back 2026-11-03", date(2026, 11, 3)),
    ("Away until Friday", date(2026, 10, 16)),
    ("Out until Monday", date(2026, 10, 19)),               # never today: the next Monday
    ("returning in 2 weeks", date(2026, 10, 26)),
    ("back next week", date(2026, 10, 19)),
    ("back 3 January", date(2027, 1, 3)),                   # a past month means next year
    ("Out of office, back 19 October 2026", date(2026, 10, 19)),
])
def test_return_dates_are_read_from_the_reply(text, expected) -> None:
    assert return_date(UntrustedText.wrap("reply", "m", text), TODAY) == expected


@pytest.mark.parametrize("text", ["I am out of office.", "hello", "back 31 February", "",
                                  "Back 2020-01-01"])
def test_no_usable_date_gives_none(text) -> None:
    assert return_date(UntrustedText.wrap("reply", "m", text), TODAY) is None


def test_snippets_return_only_the_matches_not_the_text() -> None:
    u = UntrustedText.wrap("reply", "m", "secret words 19 October more secret words")
    assert u.snippets(r"\d+ October") == ["19 October"]
