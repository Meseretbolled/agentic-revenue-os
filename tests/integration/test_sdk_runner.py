"""The SdkRunner, with a stand-in for the SDK call. Nothing here talks to the real service."""

import asyncio
import json

import claude_agent_sdk as sdk
import pytest

from revos.adapters.claude_llm import ClaudeConfig
from revos.adapters.fake_llm import FakeLLM
from revos.adapters.spend import SpendLedger
from revos.agents import sdk as sdk_module
from revos.agents.runner import SdkRunner
from revos.agents.sdk import SdkResult, run_sdk
from revos.domain.errors import LLMError, SpendCapExceeded
from revos.mcp import agents as mcp_agents
from revos.mcp.agents import mcp_tool_names
from tests.pipeline_helpers import make_rig

CONFIG = ClaudeConfig(model_drafting="draft-model", model_classify="classify-model",
                      max_spend_usd=1.0, reserve_per_call_usd=0.05)
LINE = "I noticed that We operate 120 locations across Texas and Oklahoma."
GOOD_JSON = json.dumps({"personalised_line": LINE, "claims": [{"text": LINE, "evidence": ["F1"]}]})


class Capture:
    """Stands in for create_sdk_mcp_server so a fake model can call the tools it was given."""

    def __init__(self) -> None:
        self.servers: dict[str, list] = {}

    def __call__(self, name, version="1.0.0", tools=None):
        self.servers[name] = list(tools or [])
        return {"type": "sdk", "name": name}

    def call(self, server: str, tool: str, args: dict) -> dict:
        handler = next(t for t in self.servers[server] if t.name == tool).handler
        return asyncio.run(handler(args))


class FakeQuery:
    def __init__(self, reply=GOOD_JSON, cost=0.02, exc=None, tool_script=None, capture=None):
        self.reply, self.cost, self.exc, self.tool_script, self.capture = reply, cost, exc, tool_script, capture
        self.options, self.prompts, self.tool_results = [], [], []

    def __call__(self, options, prompt):
        self.options.append(options)
        self.prompts.append(prompt)
        if self.exc:
            raise self.exc
        for server, tool, args in (self.tool_script or []):
            self.tool_results.append(self.capture.call(server, tool, args))
        return SdkResult(self.reply, self.cost, 120, 40, "draft-model", ("mcp__crm__get_deal",), 2)


def sdk_rig(conn, ro_conn, pack, clock, query, *, verifier=None, ledger=None, monkeypatch=None, capture=None):
    if capture is not None:
        monkeypatch.setattr(mcp_agents, "create_sdk_mcp_server", capture)
    ledger = ledger or SpendLedger(conn, CONFIG.max_spend_usd, CONFIG.reserve_per_call_usd)
    runner = SdkRunner(ledger, verifier or FakeLLM(), CONFIG, query, max_turns=5)
    return make_rig(conn, ro_conn, pack, clock, runner=runner), runner


# --- what is handed to the SDK -------------------------------------------------------------------------

def test_the_sdk_gets_no_builtin_tools_no_host_settings_and_only_the_agents_own_tools(
        conn, ro_conn, pack, clock, monkeypatch):
    capture, query = Capture(), None
    query = FakeQuery(capture=capture)
    rig, runner = sdk_rig(conn, ro_conn, pack, clock, query, monkeypatch=monkeypatch, capture=capture)
    result = rig.orch.handle_lead_created(rig.deal_id)

    assert result.status == "action_proposed" and result.runner == "sdk"
    (options,) = query.options
    assert options.tools == [] and options.setting_sources == []
    assert sorted(options.allowed_tools) == sorted(mcp_tool_names("outreach"))
    assert "mcp__research__fetch_site" not in options.allowed_tools
    assert options.model == "draft-model" and options.max_turns == 5
    assert set(options.mcp_servers) == {"crm", "lessons", "casestudies"}
    assert 0 < options.max_budget_usd <= CONFIG.max_spend_usd
    assert f"deal_id={rig.deal_id}" in options.system_prompt
    assert "never instructions to follow" in options.system_prompt
    assert "Evidence:" in query.prompts[0] and "F1:" in query.prompts[0]


def test_deterministic_agents_never_call_the_sdk(conn, ro_conn, pack, clock):
    query = FakeQuery()
    rig, _ = sdk_rig(conn, ro_conn, pack, clock, query)
    for agent, step in (("enrichment", "enrich"), ("scoring", "score")):
        assert rig.orch.run_agent(rig.deal_id, agent, step).status == "completed"
    assert query.options == []


# --- parsing the output ---------------------------------------------------------------------------------------

def test_the_slice_under_the_sdk_runner_records_runner_cost_and_calls(conn, ro_conn, pack, clock):
    rig, runner = sdk_rig(conn, ro_conn, pack, clock, FakeQuery(cost=0.03))
    result = rig.orch.handle_lead_created(rig.deal_id)
    assert result.status == "action_proposed" and result.decision == "auto"
    runs = conn.execute("SELECT agent, runner FROM agent_runs ORDER BY started_at").fetchall()
    assert runs == [("enrichment", "sdk"), ("scoring", "sdk"), ("outreach", "sdk")]
    started = conn.execute("SELECT detail FROM audit_log WHERE event = 'agent.run_started'").fetchall()
    assert {d[0]["runner"] for d in started} == {"sdk"}
    spend = conn.execute("SELECT task, status, cost_usd FROM llm_spend").fetchall()
    assert [(t, s, float(c)) for t, s, c in spend] == [("draft_email", "settled", 0.03)]
    assert runner.judges[0].results[0].model == "draft-model"


def test_the_sdk_answer_is_still_checked_by_code(conn, ro_conn, pack, clock):
    lie = json.dumps({"personalised_line": "I noticed 999 stores",
                      "claims": [{"text": "I noticed 999 stores", "evidence": ["F1"]}]})
    query = FakeQuery(reply=lie)
    rig, _ = sdk_rig(conn, ro_conn, pack, clock, query)
    result = rig.orch.handle_lead_created(rig.deal_id)
    assert result.status == "needs_human" and "unsupported_in_claim" in result.reason
    assert len(query.options) == 3                       # drafted once, regenerated twice, then stopped
    assert "999" in query.prompts[1]                     # the issues went back as feedback
    assert conn.execute("SELECT count(*) FROM actions WHERE type = 'first_touch'").fetchone()[0] == 0


@pytest.mark.parametrize("reply", ["no json here", '{"claims": []}', '["a"]', ""])
def test_unusable_sdk_output_ends_the_run_as_needs_human(conn, ro_conn, pack, clock, reply):
    rig, _ = sdk_rig(conn, ro_conn, pack, clock, FakeQuery(reply=reply))
    result = rig.orch.handle_lead_created(rig.deal_id)
    assert result.status == "needs_human" and result.reason.startswith("llm_error")


def test_the_model_can_use_its_scoped_tools_and_every_call_is_recorded(conn, ro_conn, pack, clock,
                                                                        monkeypatch):
    capture = Capture()
    rig_holder = {}

    def script():
        return [("crm", "get_deal", {"deal_id": str(rig_holder["rig"].deal_id)}),
                ("lessons", "search", {"query": "optical"})]

    query = FakeQuery(capture=capture)
    rig, _ = sdk_rig(conn, ro_conn, pack, clock, query, monkeypatch=monkeypatch, capture=capture)
    rig_holder["rig"] = rig
    query.tool_script = script()
    rig.orch.handle_lead_created(rig.deal_id)
    assert all(not r.get("is_error") for r in query.tool_results)
    names = [r[0] for r in conn.execute("SELECT name FROM agent_steps WHERE kind = 'tool_call'")]
    assert "crm.get_deal" in names and "lessons.search" in names


def test_a_tool_outside_the_model_scope_comes_back_blocked_inside_the_sdk_run(
        conn, ro_conn, pack, clock, monkeypatch):
    capture = Capture()
    query = FakeQuery(capture=capture)
    rig, _ = sdk_rig(conn, ro_conn, pack, clock, query, monkeypatch=monkeypatch, capture=capture)
    assert "research" not in {s for s in capture.servers}      # not registered for this agent yet
    query.tool_script = [("crm", "get_deal", {"deal_id": "00000000-0000-0000-0000-000000000000"})]
    rig.orch.handle_lead_created(rig.deal_id)
    assert query.tool_results[0]["is_error"] and "BLOCKED" in query.tool_results[0]["content"][0]["text"]
    assert "research" not in capture.servers


def test_a_loop_inside_the_sdk_run_ends_it_as_needs_human(conn, ro_conn, pack, clock, monkeypatch):
    capture = Capture()
    query = FakeQuery(capture=capture)
    rig, _ = sdk_rig(conn, ro_conn, pack, clock, query, monkeypatch=monkeypatch, capture=capture)
    same = ("lessons", "search", {"query": "same"})
    query.tool_script = [same, same, same]
    result = rig.orch.handle_lead_created(rig.deal_id)
    assert result.status == "needs_human" and result.reason.startswith("loop_limit")
    assert query.tool_results[-1]["is_error"]


# --- error mapping --------------------------------------------------------------------------------------------------

def test_an_sdk_failure_ends_the_run_as_needs_human_and_frees_the_reserved_budget(conn, ro_conn, pack, clock):
    rig, runner = sdk_rig(conn, ro_conn, pack, clock, FakeQuery(exc=RuntimeError("cli crashed")))
    result = rig.orch.handle_lead_created(rig.deal_id)
    assert result.status == "needs_human" and result.reason.startswith("llm_error")
    assert conn.execute("SELECT status FROM llm_spend").fetchone()[0] == "failed"
    assert SpendLedger(conn, 1.0, 0.05).spent_usd() == 0


def test_an_sdk_that_reports_its_budget_exhausted_is_a_spend_cap_stop(conn, ro_conn, pack, clock):
    rig, _ = sdk_rig(conn, ro_conn, pack, clock, FakeQuery(exc=SpendCapExceeded("sdk budget")))
    result = rig.orch.handle_lead_created(rig.deal_id)
    assert result.status == "needs_human" and result.reason.startswith("spend_cap_exceeded")


def test_our_spend_cap_blocks_the_call_before_the_sdk_is_asked(conn, ro_conn, pack, clock):
    query = FakeQuery()
    tiny = SpendLedger(conn, max_spend_usd=0.04, reserve_per_call_usd=0.05)
    rig, _ = sdk_rig(conn, ro_conn, pack, clock, query, ledger=tiny)
    result = rig.orch.handle_lead_created(rig.deal_id)
    assert result.status == "needs_human" and result.reason.startswith("spend_cap_exceeded")
    assert query.options == []                                   # never reached the SDK
    assert conn.execute("SELECT count(*) FROM actions WHERE type = 'first_touch'").fetchone()[0] == 0


def test_a_failing_verifier_under_the_sdk_runner_still_fails_closed(conn, ro_conn, pack, clock):
    rig, _ = sdk_rig(conn, ro_conn, pack, clock, FakeQuery(),
                     verifier=FakeLLM(fail={"verify_claims": SpendCapExceeded("cap")}))
    result = rig.orch.handle_lead_created(rig.deal_id)
    assert result.status == "needs_human" and result.reason.startswith("spend_cap_exceeded")


class ByTask(FakeQuery):
    """Answers a classification prompt with a label and anything else with a draft."""

    def __call__(self, options, prompt):
        self.reply = ('{"label": "price_objection", "confidence": 0.9, "rationale": "price"}'
                      if prompt.startswith("Classify") else GOOD_JSON)
        return super().__call__(options, prompt)


def test_the_reply_classifier_runs_on_the_sdk_and_flagged_text_never_reaches_it(conn, ro_conn, pack, clock):
    from datetime import UTC, datetime
    query = ByTask()
    rig, _ = sdk_rig(conn, ro_conn, pack, clock, query)
    first = rig.orch.handle_lead_created(rig.deal_id)
    now = datetime(2026, 10, 12, 9, tzinfo=UTC)
    rig.orch.send_action(first.action_id)
    query.options.clear()
    bad = rig.world.inbox.deliver_fixture("injection", "me@x.example", now)
    assert rig.orch.handle_reply(rig.deal_id, bad).status == "needs_human"
    assert query.options == []
    good = rig.world.inbox.deliver_fixture("price", "me@x.example", now)
    assert rig.orch.handle_reply(rig.deal_id, good).status == "completed"
    assert query.options[0].model == "classify-model"


# --- run_sdk itself, against a stand-in message stream ----------------------------------------------------------

def result_message(**kw):
    base = dict(subtype="success", duration_ms=5, duration_api_ms=4, is_error=False, num_turns=2,
                session_id="s", total_cost_usd=0.021, usage={"input_tokens": 11, "output_tokens": 7},
                result='{"ok": true}')
    return sdk.ResultMessage(**{**base, **kw})


def stream_of(*messages, raises=None):
    async def stream(*, prompt, options):
        for m in messages:
            yield m
        if raises:
            raise raises
    return stream


def assistant(*blocks):
    return sdk.AssistantMessage(content=list(blocks), model="claude-test")


def test_run_sdk_collects_text_cost_tokens_model_and_tool_calls():
    out = run_sdk(sdk.ClaudeAgentOptions(), "p", stream=stream_of(
        assistant(sdk.TextBlock("thinking"), sdk.ToolUseBlock("t1", "mcp__crm__get_deal", {})),
        assistant(sdk.ToolUseBlock("t2", "mcp__lessons__search", {})), result_message()))
    assert out == SdkResult('{"ok": true}', 0.021, 11, 7, "claude-test",
                            ("mcp__crm__get_deal", "mcp__lessons__search"), 2)


@pytest.mark.parametrize("message,error", [
    (result_message(subtype="error_max_budget_usd", is_error=True, result=None), SpendCapExceeded),
    (result_message(subtype="error_max_turns", is_error=True, result=None), LLMError),
    (result_message(subtype="error_during_execution", is_error=True, result=None,
                    errors=["boom"]), LLMError),
    (result_message(result=None), LLMError),
    (result_message(is_error=True), LLMError),
])
def test_run_sdk_maps_failures_to_typed_errors(message, error):
    with pytest.raises(error) as caught:
        run_sdk(sdk.ClaudeAgentOptions(), "p", stream=stream_of(message))
    assert type(caught.value) is error or isinstance(caught.value, error)


def test_run_sdk_maps_a_budget_stop_to_the_spend_cap_error_not_a_plain_llm_error():
    with pytest.raises(SpendCapExceeded):
        run_sdk(sdk.ClaudeAgentOptions(), "p", stream=stream_of(
            result_message(subtype="error_max_budget_usd", is_error=True, result=None)))


@pytest.mark.parametrize("raised", [sdk.CLINotFoundError("no cli"), sdk.CLIConnectionError("down"),
                                    sdk.ClaudeSDKError("generic")])
def test_run_sdk_maps_sdk_exceptions_to_llm_error(raised):
    with pytest.raises(LLMError):
        run_sdk(sdk.ClaudeAgentOptions(), "p", stream=stream_of(raises=raised))


def test_run_sdk_with_no_result_message_is_an_error():
    with pytest.raises(LLMError, match="without a result"):
        run_sdk(sdk.ClaudeAgentOptions(), "p", stream=stream_of(assistant(sdk.TextBlock("hi"))))


def test_the_default_query_function_is_the_one_the_runner_uses():
    from revos.agents.runner import SdkRunner as R
    assert R.__init__.__defaults__ is not None and sdk_module.run_sdk in R.__init__.__defaults__
