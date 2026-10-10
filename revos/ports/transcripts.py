from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True)
class Transcript:
    id: str
    title: str
    text: str                   # untrusted: wrap in UntrustedText before any model sees it
    participants: tuple[str, ...]
    ended_at: datetime


class Transcripts(Protocol):
    def get(self, transcript_id: str) -> Transcript | None: ...

    def list_ready(self) -> list[str]: ...
