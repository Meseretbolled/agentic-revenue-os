from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Post:
    channel: str
    text: str
    idempotency_key: str
    ts: str


class MockChat:
    """Records every post, including duplicates."""

    def __init__(self) -> None:
        self.posts: list[Post] = []
        self._by_key: dict[str, str] = {}

    def post(self, channel: str, text: str, idempotency_key: str) -> str:
        ts = f"1760000000.{len(self.posts):06d}"
        self.posts.append(Post(channel, text, idempotency_key, ts))
        self._by_key[idempotency_key] = ts
        return ts

    def find_by_key(self, idempotency_key: str) -> str | None:
        return self._by_key.get(idempotency_key)

    # --- ActionProvider ---
    def find(self, idempotency_key: str) -> str | None:
        return self.find_by_key(idempotency_key)

    def perform(self, action: Mapping[str, Any]) -> str:
        if action["type"] != "handoff":
            raise ValueError(f"chat cannot perform {action['type']!r}")
        payload = action["payload"]
        return self.post(payload["channel"], payload["text"], action["idempotency_key"])
