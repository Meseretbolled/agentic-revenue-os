"""System switches that live in the database so flipping them needs no restart or deploy."""

import psycopg
from psycopg.types.json import Jsonb

from revos.domain.enums import AutonomyMode
from revos.policy.audit import write_audit


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
