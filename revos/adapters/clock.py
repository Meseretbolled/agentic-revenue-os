from datetime import UTC, datetime, timedelta


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class FakeClock:
    """Test clock: time only moves when the test says so."""

    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime(2026, 10, 12, 9, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self._now

    def set(self, moment: datetime) -> None:
        self._now = moment

    def advance(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)
