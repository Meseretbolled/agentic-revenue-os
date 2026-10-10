from datetime import datetime, timedelta

from revos.adapters.mock.fixtures import load
from revos.ports.calendar import Slot

_DAY_START, _DAY_END = 9, 17      # UTC working day, Monday to Friday


class MockCalendar:
    def __init__(self) -> None:
        self._busy = [Slot(datetime.fromisoformat(b["start"]), datetime.fromisoformat(b["end"]))
                      for b in load("calendar")["busy"]]
        self.invites: list[tuple[str, str, datetime, tuple[str, ...]]] = []
        self._by_key: dict[str, str] = {}

    def _taken(self, start: datetime, end: datetime) -> bool:
        booked = [Slot(i[2], i[2] + timedelta(minutes=30)) for i in self.invites]
        return any(start < b.end and b.start < end for b in [*self._busy, *booked])

    def free_slots(self, start: datetime, end: datetime, minutes: int) -> list[Slot]:
        slots: list[Slot] = []
        cursor = start.replace(minute=0, second=0, microsecond=0)
        while cursor + timedelta(minutes=minutes) <= end:
            stop = cursor + timedelta(minutes=minutes)
            in_hours = (cursor.weekday() < 5 and cursor >= start
                        and _DAY_START <= cursor.hour and stop.hour + stop.minute / 60 <= _DAY_END)
            if in_hours and not self._taken(cursor, stop):
                slots.append(Slot(cursor, stop))
            cursor += timedelta(minutes=30)
        return slots

    def create_invite(self, idempotency_key: str, title: str, start: datetime,
                      attendees: tuple[str, ...]) -> str:
        if idempotency_key not in self._by_key:
            self._by_key[idempotency_key] = f"evt-{len(self._by_key) + 1}"
            self.invites.append((idempotency_key, title, start, attendees))
        return self._by_key[idempotency_key]

    def find_invite(self, idempotency_key: str) -> str | None:
        return self._by_key.get(idempotency_key)
