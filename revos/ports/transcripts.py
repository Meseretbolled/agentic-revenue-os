"""Call transcripts, shaped like Fireflies' API so the real adapter is a drop-in replacement.

Fireflies returns, for a transcript: id, title, date, participants and `sentences`, each with an
index, speaker_name, text and start/end times. We keep exactly those.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True)
class Sentence:
    index: int
    speaker_name: str
    text: str                    # untrusted
    start_time: float = 0.0
    end_time: float = 0.0


@dataclass(frozen=True)
class Transcript:
    id: str
    title: str
    date: datetime
    participants: tuple[str, ...]          # Fireflies gives email addresses
    sentences: tuple[Sentence, ...]
    summary: str = ""                       # Fireflies' own overview, if any (untrusted)

    @property
    def text(self) -> str:
        """One 'Speaker: sentence' line per sentence. Untrusted: wrap before any model sees it."""
        return "\n".join(f"{s.speaker_name}: {s.text}" for s in self.sentences)


@dataclass(frozen=True)
class TranscriptInfo:
    """What a listing shows: no content."""

    id: str
    title: str
    date: datetime


class Transcripts(Protocol):
    def get(self, transcript_id: str) -> Transcript | None: ...

    def list_ready(self) -> list[str]:
        """Ids of finished transcripts."""
        ...

    def list_recent(self) -> list[TranscriptInfo]:
        """Recent finished transcripts, titles and dates only."""
        ...
