"""Which integrations run for real and which are mocks, decided from the environment alone.

The rule: a real adapter is used whenever its keys exist; the mock is used only when they are
missing. There is no fallback in between: if a key exists and the service then fails, the real
adapter raises (and `revos doctor` fails). It never swaps in a mock quietly.

Some services have keys in .env but no adapter in the app yet; they honestly run as MOCK."""

import os
from collections.abc import Mapping
from dataclasses import dataclass

REAL, MOCK = "REAL", "MOCK"


@dataclass(frozen=True)
class Mode:
    service: str            # the same names `revos doctor` prints
    mode: str               # REAL or MOCK
    why: str = ""
    built: bool = True      # False: the app has no adapter for this service yet, whatever the key says

    def line(self) -> str:
        return f"{self.service:<14}{self.mode}" + (f"  ({self.why})" if self.why else "")

    def line_with(self, status: str | None, detail: str = "") -> str:
        """The line for the banner and `revos doctor`, given the service's check result (its status
        text, e.g. 'REAL OK', and detail), if one was run."""
        if not self.built and status is not None:
            if status == "REAL OK":
                return f"{self.service:<14}key OK, adapter not built yet: runs as MOCK"
            if status.startswith("REAL FAILED"):
                return (f"{self.service:<14}key NOT working ({detail or 'failed'}), adapter not built yet: "
                        "runs as MOCK")
        return self.line()


def _has(env: Mapping[str, str], *names: str) -> bool:
    return all(env.get(n, "").strip() for n in names)


def hubspot_configured(env: Mapping[str, str] | None = None) -> bool:
    return _has(os.environ if env is None else env, "HUBSPOT_TOKEN")


def gmail_keys(env: Mapping[str, str] | None = None) -> bool:
    return _has(os.environ if env is None else env, "GMAIL_ADDRESS", "GMAIL_APP_PASSWORD")


def slack_configured(env: Mapping[str, str] | None = None) -> bool:
    return _has(os.environ if env is None else env, "SLACK_BOT_TOKEN")


def llm_live(env: Mapping[str, str] | None = None) -> bool:
    env = os.environ if env is None else env
    return env.get("LLM_MODE", "fake") == "live" and _has(env, "ANTHROPIC_API_KEY")


def integration_modes(env: Mapping[str, str] | None = None) -> list[Mode]:
    env = os.environ if env is None else env

    def not_built(name: str, key: str, step: str) -> Mode:
        return Mode(name, MOCK, f"key found, adapter not built yet (arrives in {step})" if _has(env, key)
                    else "no key", built=False)

    llm_why = ("" if llm_live(env) else "LLM_MODE is fake" if env.get("LLM_MODE", "fake") != "live"
               else "LLM_MODE=live but ANTHROPIC_API_KEY is missing: the app refuses to start")
    return [
        Mode("Postgres", REAL),
        Mode("LLM budget", REAL if llm_live(env) else MOCK, llm_why),
        Mode("HubSpot", REAL if hubspot_configured(env) else MOCK,
             "" if hubspot_configured(env) else "no key"),
        Mode("Slack", REAL if slack_configured(env) else MOCK, "" if slack_configured(env) else "no key"),
        Mode("Gmail", REAL if gmail_keys(env) else MOCK, "" if gmail_keys(env) else "no key"),
        not_built("Fireflies", "FIREFLIES_API_KEY", "Step 11"),
        not_built("Dropbox Sign", "DROPBOX_SIGN_API_KEY", "Step 12"),
        not_built("Tavily", "TAVILY_API_KEY", "a later step"),
        Mode("Tunnel URL", REAL if _has(env, "PUBLIC_BASE_URL") else MOCK, "" if _has(env, "PUBLIC_BASE_URL")
             else "PUBLIC_BASE_URL is not set: Slack buttons will not work"),
    ]


def mode_of(service: str, env: Mapping[str, str] | None = None) -> str:
    return next(m.mode for m in integration_modes(env) if m.service == service)
