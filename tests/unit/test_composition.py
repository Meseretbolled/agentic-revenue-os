import pytest

from revos.agents.runner import RulesRunner, SdkRunner
from revos.composition import build_runner


def test_the_default_runner_is_the_deterministic_one(monkeypatch) -> None:
    monkeypatch.delenv("LLM_MODE", raising=False)
    runner = build_runner()
    assert isinstance(runner, RulesRunner) and runner.name == "rules"


def test_live_mode_without_a_key_fails_loudly_instead_of_falling_back(monkeypatch) -> None:
    monkeypatch.setenv("LLM_MODE", "live")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(ValueError, match="ANTHROPIC_API_KEY"):
        build_runner()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "   ")
    with pytest.raises(ValueError, match="refusing to fall back"):
        build_runner()


def test_live_mode_needs_the_database_for_the_spend_cap(monkeypatch) -> None:
    monkeypatch.setenv("LLM_MODE", "live")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "placeholder-not-a-real-key")
    with pytest.raises(ValueError, match="spend ledger"):
        build_runner(None)


def test_live_mode_with_a_key_and_a_ledger_builds_the_sdk_runner(monkeypatch, conn) -> None:
    monkeypatch.setenv("LLM_MODE", "live")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "placeholder-not-a-real-key")
    runner = build_runner(conn)
    assert isinstance(runner, SdkRunner) and runner.name == "sdk"


def test_an_unknown_mode_is_an_error(monkeypatch) -> None:
    monkeypatch.setenv("LLM_MODE", "turbo")
    with pytest.raises(ValueError, match="fake"):
        build_runner()
