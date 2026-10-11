"""Observability: the correlation id, structured JSON logs, and the redaction that keeps secrets out.

* One correlation id follows an event through the queue, the agents, the policy engine and the
  outbox. It lives in a context variable (for logs) and in a database session setting (so rows
  written while a job runs carry it by default).
* Logs are one JSON object per line in var/logs/revos.jsonl. Nothing is written until
  configure_logging() is called, so tests and library use stay silent.
* Every string is redacted before it is written: anything that looks like a key, token, app
  password or Authorization header, and the exact value of every secret in the environment.
"""

import json
import os
import re
import threading
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg

REDACTED = "[redacted]"
DEFAULT_LOG_FILE = Path(__file__).resolve().parent.parent / "var" / "logs" / "revos.jsonl"

_correlation: ContextVar[str | None] = ContextVar("revos_correlation_id", default=None)


def new_correlation_id() -> str:
    return uuid.uuid4().hex[:12]


def current_correlation() -> str | None:
    return _correlation.get()


@contextmanager
def correlation(cid: str | None, conn: psycopg.Connection | None = None) -> Iterator[str | None]:
    """Run a block under this correlation id. With a connection, the id is also set on the database
    session so rows inserted meanwhile get it by default."""
    token = _correlation.set(cid)
    if conn is not None:
        _set_session(conn, cid)
    try:
        yield cid
    finally:
        _correlation.reset(token)
        if conn is not None:
            _set_session(conn, None)


def _set_session(conn: psycopg.Connection, cid: str | None) -> None:
    try:
        conn.execute("SELECT set_config('revos.correlation_id', %s, false)", (cid or "",))
    except psycopg.Error:
        pass                      # a broken connection is the caller's problem, not the logger's


# --- redaction ---------------------------------------------------------------------------

_SAFE_NAMES = {"idempotency_key", "revos_key", "x-revos-key", "provider_key", "message_key", "cache_key",
               "sort_key", "primary_key"}
_PATTERNS = [
    re.compile(r"(?i)(authorization)(\"?\s*[:=]\s*\"?)(?:(?:bearer|basic|token)\s+)?[^\s\",;}]+"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"\bsk-ant-[A-Za-z0-9_-]{6,}"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"),
    re.compile(r"\bpat-[a-z0-9]+-[0-9a-f-]{12,}"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{6,}"),
    re.compile(r"\btvly-[A-Za-z0-9_-]{6,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
]
_ASSIGNMENT = re.compile(
    r"(?i)([\w-]*(?:api[_-]?key|token|secret|passw(?:or)?d|passwd|pwd|passphrase|credential|signing[_-]?key|"
    r"[_-]key)[\w-]*)(\"?\s*[:=]\s*\"?)([^\s\",;&}]+)")
_SECRET_NAME = re.compile(r"(?i)(key|token|secret|pass|pwd|credential)")
_secrets: set[str] = set()
_lock = threading.Lock()


def register_secrets(env: Mapping[str, str] | None = None) -> int:
    """Remember the exact value of every secret-looking environment variable, so it is removed even if
    it has no recognisable shape (an app password, say). Returns how many were registered."""
    env = os.environ if env is None else env
    found = set()
    for name, value in env.items():
        value = (value or "").strip()
        if len(value) >= 6 and _SECRET_NAME.search(name) and not name.startswith(("REVOS_", "PYTEST")):
            found |= {value, value.replace(" ", "")}
    with _lock:
        _secrets.update(v for v in found if len(v) >= 6)
    return len(found)


def clear_secrets() -> None:
    with _lock:
        _secrets.clear()


def _assignment(match: re.Match[str]) -> str:
    if match.group(1).lower() in _SAFE_NAMES:
        return match.group(0)
    return f"{match.group(1)}{match.group(2)}{REDACTED}"


def redact(text: str) -> str:
    with _lock:
        known = sorted(_secrets, key=len, reverse=True)
    for secret in known:
        text = text.replace(secret, REDACTED)
    text = _PATTERNS[0].sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", text)
    for pattern in _PATTERNS[1:]:
        text = pattern.sub(REDACTED, text)
    return _ASSIGNMENT.sub(_assignment, text)


def redact_value(value: Any) -> Any:
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, Mapping):
        return {str(k): REDACTED if _secret_key(str(k)) and isinstance(v, str) else redact_value(v)
                for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [redact_value(v) for v in value]
    return value


def _secret_key(name: str) -> bool:
    lowered = name.lower()
    return lowered not in _SAFE_NAMES and bool(re.search(
        r"(api[_-]?key|token|secret|passw|pwd|authorization|credential)", lowered))


# --- the log file ------------------------------------------------------------------------

_sink: Path | None = None
_write_lock = threading.Lock()


def configure_logging(path: Path | str | None = None) -> Path:
    """Start writing JSON lines to `path` (default var/logs/revos.jsonl) and register the secrets."""
    global _sink
    _sink = Path(path or os.environ.get("REVOS_LOG_FILE") or DEFAULT_LOG_FILE)
    _sink.parent.mkdir(parents=True, exist_ok=True)
    register_secrets()
    return _sink


def disable_logging() -> None:
    global _sink
    _sink = None


def log(component: str, message: str, *, level: str = "info", deal_id: Any = None,
        correlation_id: str | None = None, **data: Any) -> None:
    """One structured line. A no-op until configure_logging() has been called."""
    if _sink is None:
        return
    record = {"ts": datetime.now(UTC).isoformat(timespec="milliseconds"), "level": level,
              "component": component, "deal_id": str(deal_id) if deal_id else None,
              "correlation_id": correlation_id or current_correlation(), "message": message}
    if data:
        record["data"] = redact_value(data)
    record["message"] = redact(str(record["message"]))
    line = redact(json.dumps(record, default=str, ensure_ascii=False))
    with _write_lock, _sink.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def read_log(path: Path | str | None = None, *, deal_id: str | None = None,
             correlation_id: str | None = None, limit: int | None = None) -> list[dict[str, Any]]:
    """Log lines, oldest first, optionally for one deal or one correlation id."""
    target = Path(path or os.environ.get("REVOS_LOG_FILE") or DEFAULT_LOG_FILE)
    if not target.is_file():
        return []
    rows = []
    for raw in target.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(raw)
        except ValueError:
            continue
        if deal_id and row.get("deal_id") != deal_id:
            continue
        if correlation_id and row.get("correlation_id") != correlation_id:
            continue
        rows.append(row)
    return rows[-limit:] if limit else rows
