"""The only way an agent calls a tool. Wraps the Step 6 guard with recording, replay and loop limits.

* Every call is recorded (failed ones too) in the same order it happened.
* On resume, a recorded successful call is answered from the record and not executed again.
* Loop limits: an identical call (same tool, same canonical argument hash) may happen only
  `max_identical` times, errors in a row are capped, and so is the total number of calls.
"""

import json
from collections import Counter, defaultdict, deque
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from revos.agents.store import RunStore, canonical_hash
from revos.domain.errors import LoopLimitExceeded, ToolCallBlocked
from revos.mcp.registry import ToolContext, blocked, call_tool


@dataclass(frozen=True)
class Limits:
    max_identical: int = 2
    max_consecutive_errors: int = 3
    max_calls: int = 20


class ToolBroker:
    def __init__(self, store: RunStore, run_id: UUID, agent: str, ctx: ToolContext,
                 limits: Limits | None = None) -> None:
        self._store, self._run_id, self._agent, self._ctx = store, run_id, agent, ctx
        self.limits = limits or Limits()
        self.tripped: str | None = None
        self.calls: list[tuple[str, str, bool]] = []          # (tool, args hash, ok) this segment
        self.executed = 0                                       # calls that really ran
        self.replayed = 0
        self._seen: Counter[tuple[str, str]] = Counter()
        self._consecutive_errors = 0
        self._replay: dict[tuple[str, str], deque[dict[str, Any]]] = defaultdict(deque)
        for row in store.steps(run_id):
            if row["kind"] == "tool_call" and row["ok"]:
                self._replay[(row["name"], row["args_hash"])].append(row["result"])

    def _trip(self, reason: str) -> dict[str, Any]:
        self.tripped = self.tripped or reason
        return blocked(reason)

    def invoke(self, key: str, args: dict[str, Any]) -> dict[str, Any]:
        """The raw tool result (a blocked result is returned, not raised): used by the SDK path."""
        if self.tripped:
            return blocked(self.tripped)
        digest = canonical_hash(args)
        self._seen[(key, digest)] += 1
        if self._seen[(key, digest)] > self.limits.max_identical:
            return self._trip(f"repeated identical call to {key}")

        queue = self._replay.get((key, digest))
        if queue:
            self.replayed += 1
            self.calls.append((key, digest, True))
            return queue.popleft()                               # a recorded success: not re-run

        if self._store.step_count(self._run_id) >= self.limits.max_calls:
            return self._trip(f"more than {self.limits.max_calls} tool calls in one run")
        result = call_tool(self._ctx, self._agent, key, args)
        ok = not result.get("is_error")
        self._store.record_step(self._run_id, "tool_call", key, digest, result, ok)
        self.executed += 1
        self.calls.append((key, digest, ok))
        self._consecutive_errors = 0 if ok else self._consecutive_errors + 1
        if self._consecutive_errors >= self.limits.max_consecutive_errors:
            self._trip(f"{self._consecutive_errors} tool errors in a row")
        return result

    def call(self, key: str, args: dict[str, Any]) -> dict[str, Any]:
        """The parsed result for deterministic code. Raises a typed error instead of returning one."""
        result = self.invoke(key, args)
        if self.tripped:
            raise LoopLimitExceeded(self.tripped)
        text = result["content"][0]["text"]
        if result.get("is_error"):
            raise ToolCallBlocked(f"{key}: {text}")
        return json.loads(text)
