from typing import Any


class MockActionProvider:
    """Records every call. `crash_after_perform` models a provider that accepted the
    request while our process died (or timed out) before it saw the answer."""

    def __init__(self, crash_after_perform: bool = False) -> None:
        self.crash_after_perform = crash_after_perform
        self.performed: list[str] = []            # idempotency keys, one per perform() call
        self.find_calls: list[str] = []
        self._by_key: dict[str, str] = {}

    def find(self, idempotency_key: str) -> str | None:
        self.find_calls.append(idempotency_key)
        return self._by_key.get(idempotency_key)

    def perform(self, action: dict[str, Any]) -> str:
        key = action["idempotency_key"]
        self.performed.append(key)
        provider_id = f"mock-{len(self.performed)}"
        self._by_key[key] = provider_id
        if self.crash_after_perform:
            self.crash_after_perform = False
            raise ConnectionError("connection lost after the provider accepted the request")
        return provider_id
