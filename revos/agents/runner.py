"""The AgentRunner port and its two implementations.

Both run the SAME agents against the SAME Step 6 tools through the same ToolBroker. They differ
only in where model judgement comes from:

* RulesRunner: the injected LLM (the deterministic fake). No key, no network. Used by all tests.
* SdkRunner:  the Claude Agent SDK, with only that agent's MCP tools, max_turns and the spend cap.

A deterministic agent (scoring, qualification, IO, tracking, enrichment) never calls a model under
either runner.
"""

from dataclasses import replace
from typing import Protocol
from uuid import UUID

from revos.adapters.claude_llm import ClaudeConfig
from revos.adapters.spend import SpendLedger
from revos.agents.recording import RecordingLLM
from revos.agents.sdk import QueryFn, SdkJudge, run_sdk
from revos.agents.store import RunStore
from revos.agents.types import AgentEnv, AgentOutput, AgentSpec
from revos.mcp.registry import ToolContext
from revos.ports.llm import LLM


class AgentRunner(Protocol):
    name: str          # 'rules' or 'sdk': recorded on every run and in the audit log

    def run(self, spec: AgentSpec, env: AgentEnv, *, store: RunStore, run_id: UUID,
            tool_ctx: ToolContext) -> AgentOutput: ...


class RulesRunner:
    name = "rules"

    def __init__(self, llm: LLM) -> None:
        self._llm = llm

    def run(self, spec: AgentSpec, env: AgentEnv, *, store: RunStore, run_id: UUID,
            tool_ctx: ToolContext) -> AgentOutput:
        if spec.kind == "deterministic":
            return spec.run(replace(env, runner=self.name))
        judge = RecordingLLM(self._llm, store, run_id)
        return spec.run(replace(env, runner=self.name, judge=judge, verifier=judge))


class SdkRunner:
    name = "sdk"

    def __init__(self, ledger: SpendLedger, verifier: LLM, config: ClaudeConfig | None = None,
                 query: QueryFn = run_sdk, max_turns: int = 8) -> None:
        self._ledger, self._verifier = ledger, verifier
        self._config, self._query, self._max_turns = config or ClaudeConfig.from_env(), query, max_turns
        self.judges: list[SdkJudge] = []             # kept so a smoke test can print usage

    def run(self, spec: AgentSpec, env: AgentEnv, *, store: RunStore, run_id: UUID,
            tool_ctx: ToolContext) -> AgentOutput:
        if spec.kind == "deterministic":
            return spec.run(replace(env, runner=self.name))
        hint = (f"You are working on deal_id={env.deal_id}, contact_id={env.contact_id}, "
                f"company_id={env.company_id}. Use only your tools, only with these ids.")
        judge = SdkJudge(spec, tool_ctx, env.tools, self._ledger, self._config, self._query,
                         self._max_turns, hint)
        self.judges.append(judge)
        return spec.run(replace(env, runner=self.name, judge=RecordingLLM(judge, store, run_id),
                                verifier=RecordingLLM(self._verifier, store, run_id)))
