from datetime import datetime

from revos.adapters.mock.fixtures import load
from revos.ports.transcripts import Sentence, Transcript, TranscriptInfo


class MockTranscripts:
    def __init__(self) -> None:
        self._items = {
            t["id"]: Transcript(
                t["id"], t["title"], datetime.fromisoformat(t["date"]), tuple(t["participants"]),
                tuple(Sentence(i, s["speaker_name"], s["text"], float(i), float(i) + 1)
                      for i, s in enumerate(t["sentences"])))
            for t in load("transcripts")["transcripts"]
        }
        self.fetched: list[str] = []
        self._arrived: list[str] = []         # calls that "finished" after start: what a poll sees

    def get(self, transcript_id: str) -> Transcript | None:
        self.fetched.append(transcript_id)
        return self._items.get(transcript_id)

    def list_ready(self) -> list[str]:
        return sorted(self._items)

    def list_recent(self) -> list[TranscriptInfo]:
        """What a poll sees: only calls that finished since start (see `add`), never the fixtures."""
        return [TranscriptInfo(t.id, t.title, t.date) for i in self._arrived
                if (t := self._items.get(i)) is not None]

    def add(self, transcript: Transcript) -> None:
        """For tests and `revos call-demo`: a call finishes and becomes available to a poll."""
        self._items[transcript.id] = transcript
        if transcript.id not in self._arrived:
            self._arrived.append(transcript.id)
