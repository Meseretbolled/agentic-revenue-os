"""Persistence of agent runs. Every step is written as it happens, so a crashed run resumes from
the database and a recorded success is never run again."""

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from revos.agents.types import Proposal
from revos.domain.errors import RevosError, RunnerMismatch
from revos.policy.audit import write_audit


def canonical_hash(obj: Any) -> str:
    """Stable hash of arguments: key order and spacing do not matter."""
    text = json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode()).hexdigest()[:24]


@dataclass(frozen=True)
class RunRow:
    id: UUID
    agent: str
    step: str
    runner: str
    status: str
    output: dict[str, Any] | None
    human_reason: str | None
    step_count: int
    created: bool


class RunStore:
    def __init__(self, conn: psycopg.Connection) -> None:
        self._conn = conn

    def start(self, deal_id: UUID, agent: str, step: str, runner: str,
              input_: dict[str, Any]) -> RunRow:
        key = f"run:{deal_id}:{step}"
        with self._conn.transaction():
            row = self._conn.cursor(row_factory=dict_row).execute(
                "INSERT INTO agent_runs (idempotency_key, deal_id, agent, step, runner, input) "
                "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (idempotency_key) DO NOTHING "
                "RETURNING *", (key, deal_id, agent, step, runner, Jsonb(input_))).fetchone()
            created = row is not None
            if row is None:
                row = self._conn.cursor(row_factory=dict_row).execute(
                    "SELECT * FROM agent_runs WHERE idempotency_key = %s", (key,)).fetchone()
            if row["runner"] != runner or row["agent"] != agent:
                raise RunnerMismatch(f"run {key} was started by agent {row['agent']} on the "
                                     f"{row['runner']} runner, not {agent} on {runner}")
        return RunRow(row["id"], row["agent"], row["step"], row["runner"], row["status"],
                      row["output"], row["human_reason"], row["step_count"], created)

    def steps(self, run_id: UUID) -> list[dict[str, Any]]:
        return self._conn.cursor(row_factory=dict_row).execute(
            "SELECT * FROM agent_steps WHERE run_id = %s ORDER BY idx", (run_id,)).fetchall()

    def step_count(self, run_id: UUID) -> int:
        return self._conn.execute("SELECT step_count FROM agent_runs WHERE id = %s",
                                  (run_id,)).fetchone()[0]

    def record_step(self, run_id: UUID, kind: str, name: str, args_hash: str, result: Any,
                    ok: bool) -> int:
        with self._conn.transaction():
            row = self._conn.execute("SELECT status, step_count FROM agent_runs WHERE id = %s "
                                     "FOR UPDATE", (run_id,)).fetchone()
            if row is None or row[0] != "running":
                raise RevosError("cannot record a step on a run that is not running")
            idx = row[1] + 1
            self._conn.execute(
                "INSERT INTO agent_steps (run_id, idx, kind, name, args_hash, result, ok) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (run_id, idx, kind, name, args_hash, Jsonb(result), ok))
            self._conn.execute("UPDATE agent_runs SET step_count = %s WHERE id = %s", (idx, run_id))
        return idx

    def finish(self, run_id: UUID, deal_id: UUID, status: str, output: dict[str, Any],
               human_reason: str | None, proposals: Sequence[Proposal]) -> bool:
        """Proposals and the final status are written in ONE transaction. False if already done."""
        with self._conn.transaction():
            row = self._conn.execute("SELECT status, runner, agent, step FROM agent_runs "
                                     "WHERE id = %s FOR UPDATE", (run_id,)).fetchone()
            if row[0] != "running":
                return False
            for i, p in enumerate(proposals):
                self._conn.execute(
                    "INSERT INTO agent_proposals (run_id, idx, action_type, payload, evidence_ids, "
                    "reason) VALUES (%s, %s, %s, %s, %s, %s)",
                    (run_id, i, p.action_type,
                     Jsonb({**p.payload, "_contact_id": str(p.target.contact_id)
                            if p.target.contact_id else None}),
                     list(p.evidence_ids), p.reason))
            self._conn.execute(
                "UPDATE agent_runs SET status = %s, output = %s, human_reason = %s, "
                "finished_at = now() WHERE id = %s",
                (status, Jsonb(output), human_reason, run_id))
            write_audit(self._conn, "agent", f"agent.run_{status}", deal_id, {
                "run_id": str(run_id), "agent": row[2], "step": row[3], "runner": row[1],
                "proposals": len(proposals), "reason": human_reason})
        return True

    def proposals(self, run_id: UUID) -> list[dict[str, Any]]:
        return self._conn.cursor(row_factory=dict_row).execute(
            "SELECT * FROM agent_proposals WHERE run_id = %s ORDER BY idx", (run_id,)).fetchall()
