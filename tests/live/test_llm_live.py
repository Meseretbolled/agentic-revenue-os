"""LIVE: one whole lead (first touch, reply, slot, call, qualification, IO) with the REAL model, against the
mock mail, CRM and e-sign adapters, so nothing is sent. Skipped unless run with -m live (`make live-llm`).
It measures what a lead costs, per task, from the spend ledger of the test database, and it stops at
LIVE_LEAD_MAX_USD (default 0.40)."""

import os

import pytest

from revos.adapters.claude_llm import ClaudeConfig, ClaudeLLM
from revos.adapters.spend import SpendLedger
from revos.agents.runner import SdkRunner
from revos.env import load_env_file
from tests.integration.test_close_the_deal import qualified, send_all
from tests.integration.test_io_auto import lead_record

pytestmark = pytest.mark.live


def test_one_lead_end_to_end_with_the_real_model(conn, ro_conn, pack, clock):
    os.environ.pop("REVOS_NO_DOTENV", None)
    load_env_file()
    if not os.environ.get("ANTHROPIC_API_KEY", "").strip():
        pytest.skip("ANTHROPIC_API_KEY is not set")
    cap = float(os.environ.get("LIVE_LEAD_MAX_USD", "0.40"))
    base = ClaudeConfig.from_env()
    config = ClaudeConfig(base.model_drafting, base.model_classify, max_spend_usd=cap,
                          reserve_per_call_usd=base.reserve_per_call_usd)
    conn.autocommit = True
    llm = ClaudeLLM(conn, config)
    runner = SdkRunner(SpendLedger(conn, cap, config.reserve_per_call_usd), llm, config)

    rig = qualified(conn, ro_conn, pack, clock, llm=llm, runner=runner)
    lead_record(conn)
    result = rig.orch.handle_io(rig.deal_id)
    send_all(rig)

    rows = conn.execute(
        "SELECT task, model, count(*), sum(input_tokens), sum(output_tokens), sum(cost_usd) FROM llm_spend "
        "WHERE status = 'settled' GROUP BY task, model ORDER BY 6 DESC").fetchall()
    total = sum(float(r[5] or 0) for r in rows)
    print("\nLIVE LEAD RUN: stage", conn.execute("SELECT stage FROM deals").fetchone()[0], "| io:", result.status)
    for task, model, n, tin, tout, cost in rows:
        print(f"  {task:<20} {model:<18} calls={n:<2} tokens in/out={tin}/{tout}  ${float(cost):.4f}")
    print(f"  TOTAL ${total:.4f} of ${cap:.2f}; emails sent through the mock mailer: {len(rig.world.mailer.sent)}")
    assert total <= cap
