"""System switches that live in the database so flipping them needs no restart or deploy."""

from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

from revos.domain.enums import AutonomyMode
from revos.policy.audit import write_audit
from revos.ports.clock import Clock
from revos.runtime.events import record_event


def kill_switch_on(conn: psycopg.Connection) -> bool:
    row = conn.execute("SELECT value FROM settings WHERE key = 'kill_switch'").fetchone()
    return row is None or row[0] is not False      # missing setting fails closed


def autonomy_mode(conn: psycopg.Connection) -> AutonomyMode:
    row = conn.execute("SELECT value FROM settings WHERE key = 'autonomy_mode'").fetchone()
    return AutonomyMode(row[0]) if row else AutonomyMode.APPROVAL   # fails closed


def _set(conn: psycopg.Connection, key: str, value: object, actor: str, event: str) -> None:
    with conn.transaction():
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (%s, %s) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()",
            (key, Jsonb(value)),
        )
        write_audit(conn, actor, event, None, {"value": value})


def set_kill_switch(conn: psycopg.Connection, on: bool, actor: str) -> None:
    _set(conn, "kill_switch", on, actor, "system.kill_switch")


def set_autonomy_mode(conn: psycopg.Connection, mode: AutonomyMode, actor: str) -> None:
    _set(conn, "autonomy_mode", mode.value, actor, "system.autonomy_mode")


def _epoch(conn: psycopg.Connection) -> int:
    row = conn.execute("SELECT value FROM settings WHERE key = 'kill_switch_epoch'").fetchone()
    return int(row[0]) if row else 0


def release_held_sends(conn: psycopg.Connection, clock: Clock) -> list[UUID]:
    """Re-queue every send the kill switch is holding. Does nothing while the switch is on.

    Each action gets ONE resume event per epoch (key `resume:<action>:<epoch>`), so calling this
    again, from the scheduler or by hand, cannot queue a second send. The gate and the outbox
    still guard the send itself."""
    if kill_switch_on(conn):
        return []
    epoch = _epoch(conn)
    queued = []
    with conn.transaction():
        held = conn.execute("SELECT id FROM actions WHERE hold_reason = 'kill_switch' "
                            "AND status = 'approved' ORDER BY created_at, id").fetchall()
        for (action_id,) in held:
            created = record_event(conn, f"resume:{action_id}:{epoch}", "action.send",
                                   {"action_id": str(action_id)}, clock)
            if created is not None:
                queued.append(action_id)
        if queued:
            write_audit(conn, "system", "system.sends_resumed", None,
                        {"epoch": epoch, "actions": [str(a) for a in queued]})
    return queued


def resume_after_kill_switch(conn: psycopg.Connection, clock: Clock, actor: str) -> list[UUID]:
    """Turn the kill switch off and put every held send back on the queue, atomically."""
    with conn.transaction():
        set_kill_switch(conn, False, actor)
        conn.execute("UPDATE settings SET value = to_jsonb(%s::int), updated_at = now() "
                     "WHERE key = 'kill_switch_epoch'", (_epoch(conn) + 1,))
        return release_held_sends(conn, clock)
