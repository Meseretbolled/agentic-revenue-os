from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

Blocks = Sequence[dict[str, Any]]


@dataclass(frozen=True)
class Posted:
    channel: str       # Slack's channel ID (needed to update the message later)
    ts: str            # the message's timestamp id


class Slack(Protocol):
    """The few Slack calls the app makes. A real adapter and a mock share this interface."""

    def post(self, channel: str, text: str, blocks: Blocks) -> Posted:
        """Post a message; return where it landed (the channel id and ts used to update it)."""
        ...

    def update(self, channel: str, ts: str, text: str, blocks: Blocks) -> None:
        """Replace a message we posted (chat.update)."""
        ...

    def ephemeral(self, channel: str, user: str, text: str) -> None:
        """A message only `user` sees."""
        ...
