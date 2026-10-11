from dataclasses import dataclass, field
from typing import Any

from revos.ports.slack import Blocks, Posted


@dataclass
class SlackPost:
    channel: str
    ts: str
    text: str
    blocks: list[dict[str, Any]]
    history: list[tuple[str, list[dict[str, Any]]]] = field(default_factory=list)   # earlier versions


class MockSlack:
    """Records posts, in-place updates and ephemeral messages. Same interface as the real adapter."""

    def __init__(self) -> None:
        self.posts: list[SlackPost] = []
        self.updates: list[tuple[str, str]] = []                 # (ts, text)
        self.ephemerals: list[tuple[str, str, str]] = []         # (channel, user, text)
        self.fail_next: Exception | None = None

    def _maybe_fail(self) -> None:
        if self.fail_next is not None:
            exc, self.fail_next = self.fail_next, None
            raise exc

    def post(self, channel: str, text: str, blocks: Blocks) -> Posted:
        self._maybe_fail()
        ts = f"1760000000.{len(self.posts):06d}"
        self.posts.append(SlackPost(channel, ts, text, [dict(b) for b in blocks]))
        return Posted(channel, ts)

    def update(self, channel: str, ts: str, text: str, blocks: Blocks) -> None:
        self._maybe_fail()
        post = next(p for p in self.posts if p.ts == ts and p.channel == channel)
        post.history.append((post.text, post.blocks))
        post.text, post.blocks = text, [dict(b) for b in blocks]
        self.updates.append((ts, text))

    def ephemeral(self, channel: str, user: str, text: str) -> None:
        self._maybe_fail()
        self.ephemerals.append((channel, user, text))

    # helpers for tests
    def message(self, ts: str) -> SlackPost:
        return next(p for p in self.posts if p.ts == ts)

    def buttons(self, ts: str) -> list[dict[str, Any]]:
        return [e for b in self.message(ts).blocks if b["type"] == "actions" for e in b["elements"]]
