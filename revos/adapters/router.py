"""Sends each outbox action to the provider that owns its type, and looks a key up in all of them."""

from collections.abc import Mapping
from typing import Any

from revos.ports.provider import ActionProvider


class ActionRouter:
    def __init__(self, routes: Mapping[str, ActionProvider]) -> None:
        self._routes = dict(routes)

    def find(self, idempotency_key: str) -> str | None:
        seen: list[ActionProvider] = []
        for provider in self._routes.values():
            if any(provider is s for s in seen):
                continue
            seen.append(provider)
            if (hit := provider.find(idempotency_key)) is not None:
                return hit
        return None

    def perform(self, action: Mapping[str, Any]) -> str:
        provider = self._routes.get(action["type"])
        if provider is None:
            raise ValueError(f"no provider is registered for action type {action['type']!r}")
        return provider.perform(dict(action))
