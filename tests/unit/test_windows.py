from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from revos.policy.config import SendWindow
from revos.policy.windows import next_open, zone

W = SendWindow()   # 09:00-17:00, Monday to Friday


def utc(y, m, d, h, mi=0):
    return datetime(y, m, d, h, mi, tzinfo=UTC)


def test_open_inside_the_window() -> None:
    now = utc(2026, 10, 12, 10)                       # Monday 10:00 UTC
    assert next_open(now, ZoneInfo("UTC"), W) == now


def test_same_instant_is_open_in_one_zone_and_closed_in_another() -> None:
    now = utc(2026, 10, 12, 9)                        # Monday 09:00 UTC
    assert next_open(now, ZoneInfo("Europe/London"), W) == now                    # 10:00 BST
    assert next_open(now, ZoneInfo("Asia/Tokyo"), W) == utc(2026, 10, 13, 0)      # 18:00 JST: tomorrow 09:00 JST
    assert next_open(now, ZoneInfo("America/New_York"), W) == utc(2026, 10, 12, 13)  # 05:00 EDT


def test_weekend_rolls_to_monday() -> None:
    saturday = utc(2026, 10, 17, 12)
    assert next_open(saturday, ZoneInfo("UTC"), W) == utc(2026, 10, 19, 9)


def test_window_end_is_exclusive() -> None:
    assert next_open(utc(2026, 10, 12, 17), ZoneInfo("UTC"), W) == utc(2026, 10, 13, 9)


def test_unknown_zone_falls_back() -> None:
    assert str(zone("Mars/Olympus", "Europe/London")) == "Europe/London"
    assert str(zone(None, "UTC")) == "UTC"
