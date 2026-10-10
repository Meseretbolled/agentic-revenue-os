#!/usr/bin/env python
"""One small, spend-capped call through the real Claude Agent SDK.

Run it yourself when your Anthropic credit works:

    make db-up && .venv/bin/python scripts/sdk_smoke.py

It makes ONE model call (the Outreach agent's drafting task, with its own scoped tools) and prints
the model name, where the credential came from (never the key), tokens, cost and each tool call.
It stops at SMOKE_MAX_USD (default 0.25). It creates a throwaway deal and removes it afterwards.
Nothing is sent anywhere. It has NOT been run by the author of this repository.
"""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def load_env_file() -> None:
    """Fill missing variables from .env. Values are never printed."""
    path = ROOT / ".env"
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if "=" in line:
            name, value = line.split("=", 1)
            if value.strip():
                os.environ.setdefault(name.strip(), value.strip())


def credential_source() -> str:
    if os.environ.get("ANTHROPIC_API_KEY", "").strip():
        return "ANTHROPIC_API_KEY from the environment (value not shown)"
    if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "").strip():
        return "CLAUDE_CODE_OAUTH_TOKEN from the environment (value not shown)"
    return "no API key in the environment; the SDK will use the Claude Code login, if any"


def main() -> int:
    load_env_file()
    from revos.adapters.claude_llm import ClaudeConfig
    from revos.adapters.mock import MockResearch
    from revos.adapters.spend import SpendLedger
    from revos.agents.broker import ToolBroker
    from revos.agents.registry import AGENTS
    from revos.agents.sdk import SdkJudge
    from revos.agents.store import RunStore
    from revos.db import connect, connect_read_only, migrate
    from revos.domain.drafting import Evidence, EvidenceItem
    from revos.domain.errors import LLMError, RevosError
    from revos.mcp.registry import ToolContext
    from revos.mcp.scope import fixed_scope
    from revos.ports.llm import DraftRequest
    from revos.segments import load_pack

    pack = load_pack(ROOT / "packs" / "franchise_brands")
    cap = float(os.environ.get("SMOKE_MAX_USD", "0.25"))
    config = ClaudeConfig.from_env()
    print(f"model (drafting): {config.model_drafting}")
    print(f"credential source: {credential_source()}")
    print(f"spend cap for this script: ${cap:.2f}")

    conn = connect()
    conn.autocommit = True
    migrate(conn)
    ro = connect_read_only()
    ids: dict[str, object] = {}
    try:
        conn.execute("INSERT INTO segments (id, pack_version) VALUES (%s, '1') ON CONFLICT DO NOTHING",
                     (pack.id,))
        ids["company"] = conn.execute(
            "INSERT INTO companies (segment_id, name, domain) VALUES (%s, 'Smoke Test Co', "
            "'smoke-test.example') RETURNING id", (pack.id,)).fetchone()[0]
        ids["contact"] = conn.execute(
            "INSERT INTO contacts (company_id, email, full_name) VALUES (%s, 'smoke@smoke-test.example',"
            " 'Sam Lee') RETURNING id", (ids["company"],)).fetchone()[0]
        ids["deal"] = conn.execute(
            "INSERT INTO deals (segment_id, company_id, contact_id, fields) VALUES (%s, %s, %s, "
            "'{\"fact:F1\": \"Smoke Test Co operates 12 locations.\"}') RETURNING id",
            (pack.id, ids["company"], ids["contact"])).fetchone()[0]

        store = RunStore(conn)
        run = store.start(ids["deal"], "outreach", "smoke", "sdk", {"smoke": True})
        ctx = ToolContext(ro, {pack.id: pack}, MockResearch(), fixed_scope(ro, ids["deal"]))
        broker = ToolBroker(store, run.id, "outreach", ctx)
        ledger = SpendLedger(conn, cap, reserve_per_call_usd=min(0.10, cap))
        hint = (f"deal_id={ids['deal']}, contact_id={ids['contact']}, company_id={ids['company']}. "
                "First call the crm get_deal tool once for this deal, then write the line.")
        judge = SdkJudge(AGENTS["outreach"], ctx, broker, ledger, config, hint=hint, max_turns=4)
        request = DraftRequest("s", "b", {"first_name": "Sam", "company": "Smoke Test Co"},
                               Evidence.of([EvidenceItem("F1", "Smoke Test Co operates 12 locations.")]))
        try:
            proposal = judge.draft_email(request)
        except (LLMError, RevosError) as exc:
            print(f"FAILED: {type(exc).__name__}: {exc}")
            return 1
        result = judge.results[-1]
        print(f"model used: {result.model}")
        print(f"tokens: input {result.input_tokens}, output {result.output_tokens}")
        print(f"cost: ${result.cost_usd if result.cost_usd is not None else 'not reported'}")
        print(f"spent so far in the ledger: ${ledger.spent_usd():.4f}")
        print(f"turns: {result.turns}")
        print("tool calls made by the model:", list(result.tool_names) or "none")
        for tool, digest, ok in broker.calls:
            print(f"  scoped tool {tool} args#{digest} {'ok' if ok else 'BLOCKED'}")
        print(f"personalised line: {proposal.personalised_line!r}")
        print(f"claims: {[(c.text, c.evidence) for c in proposal.claims]}")
        return 0
    finally:
        if "deal" in ids:
            conn.execute("DELETE FROM agent_steps WHERE run_id IN "
                         "(SELECT id FROM agent_runs WHERE deal_id = %s)", (ids["deal"],))
            conn.execute("DELETE FROM agent_runs WHERE deal_id = %s", (ids["deal"],))
            conn.execute("DELETE FROM deals WHERE id = %s", (ids["deal"],))
            conn.execute("DELETE FROM contacts WHERE id = %s", (ids["contact"],))
            conn.execute("DELETE FROM companies WHERE id = %s", (ids["company"],))
        conn.close()
        ro.close()


if __name__ == "__main__":
    raise SystemExit(main())
