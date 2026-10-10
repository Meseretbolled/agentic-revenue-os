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


def build_runner(conn: psycopg.Connection | None = None):
    """The agent runner. Which one is active is recorded on every run and in the audit log.

    * LLM_MODE=fake (default): RulesRunner, the deterministic fake model. No key, no network.
    * LLM_MODE=live: SdkRunner on the Claude Agent SDK. Needs ANTHROPIC_API_KEY and a database
      connection for the spend cap. If either is missing this fails loudly; it never quietly falls
      back to the deterministic runner.
    """
    from revos.adapters.claude_llm import ClaudeConfig
    from revos.adapters.spend import SpendLedger
    from revos.agents.runner import RulesRunner, SdkRunner

    mode = os.environ.get("LLM_MODE", "fake")
    if mode == "fake":
        return RulesRunner(FakeLLM())
    if mode == "live":
        if not os.environ.get("ANTHROPIC_API_KEY", "").strip():
            raise ValueError("LLM_MODE=live needs ANTHROPIC_API_KEY; refusing to fall back to the "
                             "deterministic runner")
        if conn is None:
            raise ValueError("live mode needs a database connection for the spend ledger")
        config = ClaudeConfig.from_env()
        return SdkRunner(SpendLedger(conn, config.max_spend_usd, config.reserve_per_call_usd),
                         ClaudeLLM(conn, config), config)
    raise ValueError(f"LLM_MODE must be 'fake' or 'live', got '{mode}'")
