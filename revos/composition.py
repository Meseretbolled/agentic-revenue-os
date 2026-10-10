"""Composition root: the only place that picks concrete adapters."""

import os

import psycopg

from revos.adapters.claude_llm import ClaudeLLM
from revos.adapters.fake_llm import FakeLLM
from revos.ports.llm import LLM


def build_llm(conn: psycopg.Connection | None = None) -> LLM:
    """LLM_MODE=fake (default) is the offline model; LLM_MODE=live is Claude with a spend cap."""
    mode = os.environ.get("LLM_MODE", "fake")
    if mode == "fake":
        return FakeLLM()
    if mode == "live":
        if conn is None:
            raise ValueError("live mode needs a database connection for the spend ledger")
        return ClaudeLLM(conn)
    raise ValueError(f"LLM_MODE must be 'fake' or 'live', got '{mode}'")
