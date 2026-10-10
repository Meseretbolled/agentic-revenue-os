from datetime import datetime

from revos.adapters.mock.fixtures import load
from revos.ports.transcripts import Sentence, Transcript


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

    def get(self, transcript_id: str) -> Transcript | None:
        self.fetched.append(transcript_id)
        return self._items.get(transcript_id)

    def list_ready(self) -> list[str]:
        return sorted(self._items)
