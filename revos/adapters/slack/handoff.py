"""The closed-won handoff, posted to Slack as an outbox action (type `handoff`).

Idempotent through slack_messages: a retry after a crash finds the message already posted."""

from collections.abc import Callable, Mapping
from typing import Any

import psycopg

from revos.ports.slack import Slack


class SlackHandoff:
    def __init__(self, slack: Slack, channel: str, conn_factory: Callable[[], psycopg.Connection]) -> None:
        self._slack, self._channel, self._connect = slack, channel, conn_factory

    def find(self, idempotency_key: str) -> str | None:
        with self._connect() as conn:
            conn.autocommit = True
            row = conn.execute("SELECT ts FROM slack_messages WHERE kind = 'handoff' AND ref = %s",
                               (idempotency_key,)).fetchone()
        return row[0] if row else None

    def perform(self, action: Mapping[str, Any]) -> str:
        if action["type"] != "handoff":
            raise ValueError(f"the handoff channel cannot perform {action['type']!r}")
        text = str(action["payload"]["text"])
        head, _, rest = text.partition("\n")
        blocks: list[dict[str, Any]] = [
            {"type": "header", "text": {"type": "plain_text", "text": head[:150]}},
            {"type": "section", "text": {"type": "mrkdwn", "text": rest[:2900] or head}}]
        posted = self._slack.post(self._channel, head, blocks)
        with self._connect() as conn:
            conn.autocommit = True
            conn.execute("INSERT INTO slack_messages (kind, ref, channel, ts, state) "
                         "VALUES ('handoff', %s, %s, %s, 'posted') ON CONFLICT (kind, ref) DO NOTHING",
                         (action["idempotency_key"], posted.channel, posted.ts))
        return posted.ts
