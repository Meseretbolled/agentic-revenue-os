"""Tool specs, the guard every call passes through, and the shape of a tool result."""

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import psycopg

from revos.domain.untrusted import UntrustedText
from revos.mcp.scope import RunScope, ScopeProvider, ToolBlocked, resolve_scope
from revos.ports.research import Research
from revos.segments.loader import Pack


@dataclass(frozen=True)
class BlockEvent:
    agent: str
    tool: str
    reason: str
    deal_id: UUID | None


@dataclass(frozen=True)
class ToolContext:
    conn: psycopg.Connection                  # opened read-only (db.connect_read_only)
    packs: Mapping[str, Pack]
    research: Research
    scope_provider: ScopeProvider
    on_blocked: Callable[[BlockEvent], None] = lambda event: None
    on_quarantine: Callable[[UntrustedText, UUID], None] = lambda content, deal: None


Handler = Callable[[ToolContext, RunScope, dict[str, Any]], dict[str, Any]]


@dataclass(frozen=True)
class ToolSpec:
    server: str
    name: str
    description: str
    properties: dict[str, Any]
    required: tuple[str, ...]
    handler: Handler

    @property
    def key(self) -> str:
        return f"{self.server}.{self.name}"

    @property
    def schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": self.properties, "required": list(self.required),
                "additionalProperties": False}


def ok(payload: Any) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": json.dumps(payload, default=str)}]}


def blocked(message: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": f"BLOCKED: {message}"}], "is_error": True}


def call_tool(ctx: ToolContext, agent: str, key: str, args: dict[str, Any]) -> dict[str, Any]:
    """The guard. Order matters: agent scope, then run scope, then the tool's own id checks."""
    from revos.mcp.agents import REGISTRY, tools_for  # late import: agents imports registry

    scope: RunScope | None = None
    try:
        if key not in {t.key for t in tools_for(agent)}:
            raise ToolBlocked(f"tool {key} is not in the scope of agent {agent}")
        spec = REGISTRY[key]
        unknown = set(args) - set(spec.properties)
        if unknown:
            raise ToolBlocked("unexpected argument(s): " + ", ".join(sorted(unknown)))
        missing = [r for r in spec.required if r not in args]
        if missing:
            raise ToolBlocked("missing argument(s): " + ", ".join(missing))

        scope = ctx.scope_provider()
        if scope is None:
            raise ToolBlocked("the run's scope could not be resolved")
        fresh = resolve_scope(ctx.conn, scope.deal_id)       # the deal may have moved or vanished
        if fresh.segment_id != scope.segment_id:
            raise ToolBlocked("the deal no longer belongs to the run's segment")
        return ok(spec.handler(ctx, fresh, dict(args)))
    except ToolBlocked as exc:
        ctx.on_blocked(BlockEvent(agent, key, str(exc), scope.deal_id if scope else None))
        return blocked(str(exc))
    except Exception as exc:  # noqa: BLE001 - fail closed; internals are not shown to the model
        ctx.on_blocked(BlockEvent(agent, key, f"{type(exc).__name__}", scope.deal_id if scope else None))
        return blocked("the tool could not complete safely, so the call was blocked")


def fence(ctx: ToolContext, scope: RunScope, kind: Any, origin: str, text: str, *,
          is_html: bool = False, max_chars: int | None = None) -> str | None:
    """Wrap inbound-derived text. Flagged text is quarantined and never returned."""
    wrapped = UntrustedText.wrap(kind, origin, text, is_html=is_html, max_chars=max_chars)
    if wrapped.flagged:
        ctx.on_quarantine(wrapped, scope.deal_id)
        return None
    return wrapped.for_model()
