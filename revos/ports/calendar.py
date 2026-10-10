from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True)
class Slot:
    start: datetime
    end: datetime


class Calendar(Protocol):
    def free_slots(self, start: datetime, end: datetime, minutes: int) -> list[Slot]: ...

    def create_invite(self, idempotency_key: str, title: str, start: datetime,
                      attendees: tuple[str, ...]) -> str: ...

    def find_invite(self, idempotency_key: str) -> str | None: ...
