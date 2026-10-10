from typing import Protocol


class Chat(Protocol):
    def post(self, channel: str, text: str, idempotency_key: str) -> str:
        """Post a message and return its timestamp id."""
        ...

    def find_by_key(self, idempotency_key: str) -> str | None: ...
