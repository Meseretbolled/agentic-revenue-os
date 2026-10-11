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

    def provider_for(self, action_type: str) -> ActionProvider | None:
        return self._routes.get(action_type)

    def find_for(self, action: Mapping[str, Any]) -> str | None:
        """Ask only the provider that owns this action's type, so an outage at one provider (the
        CRM) can never stop an action that belongs to another (the mail server)."""
        provider = self._routes.get(action["type"])
        if provider is None:
            raise ValueError(f"no provider is registered for action type {action['type']!r}")
        return provider.find(action["idempotency_key"])

    def perform(self, action: Mapping[str, Any]) -> str:
        provider = self._routes.get(action["type"])
        if provider is None:
            raise ValueError(f"no provider is registered for action type {action['type']!r}")
        return provider.perform(dict(action))
