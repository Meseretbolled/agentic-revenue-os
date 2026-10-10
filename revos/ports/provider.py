from typing import Any, Protocol


class ActionProvider(Protocol):
    """The outside system an outbox action talks to (mail server, CRM, e-sign)."""

    def find(self, idempotency_key: str) -> str | None:
        """Provider id (message id, CRM record, envelope id) if this key was already done."""
        ...

    def perform(self, action: dict[str, Any]) -> str:
        """Do the side effect and return the provider's id for it."""
        ...
