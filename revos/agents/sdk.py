"""The Claude Agent SDK, wrapped: one call per model task, scoped tools only, spend-capped.

NOT RUN LIVE in this repository's tests (they inject a stand-in for the SDK). The option names
come from the installed claude-agent-sdk and the result handling follows its documented message
types, but no request has been made to the real service from here.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

import anyio
import claude_agent_sdk as sdk
from claude_agent_sdk import ClaudeAgentOptions

from revos.adapters.claude_llm import _UNTRUSTED_NOTE, ClaudeConfig
from revos.adapters.llm_tasks import (
    classify_prompt,
    draft_prompt,
    extract_json_object,
    extract_prompt,
    lesson_prompt,
    parse_classification,
    parse_draft,
    parse_extraction,
    parse_lesson,
)
from revos.adapters.spend import SpendLedger
from revos.agents.broker import ToolBroker
from revos.agents.types import AgentSpec
from revos.domain.drafting import Draft, Evidence
from revos.domain.errors import LLMError, LoopLimitExceeded, SpendCapExceeded
from revos.domain.untrusted import UntrustedText
from revos.mcp.agents import agent_options
from revos.mcp.registry import ToolContext
from revos.ports.llm import (
    CallExtraction,
    ClaimVerdict,
    DraftProposal,
    DraftRequest,
    LessonDraft,
    ReplyClassification,
)


@dataclass(frozen=True)
class SdkResult:
    text: str
    cost_usd: float | None
    input_tokens: int
    output_tokens: int
    model: str | None
    tool_names: tuple[str, ...]
    turns: int


QueryFn = Callable[[ClaudeAgentOptions, str], SdkResult]
Stream = Callable[..., Any]       # claude_agent_sdk.query, or a stand-in with the same shape


def _failure(subtype: str | None, detail: str) -> LLMError:
    if subtype == "error_max_budget_usd":
        return SpendCapExceeded("the SDK stopped the run at its budget limit")
    if subtype == "error_max_turns":
        return LLMError("the SDK run hit max_turns before finishing")
    return LLMError(f"the SDK run failed ({subtype or 'unknown'}): {detail}"[:300])


async def _collect(stream: Stream, options: ClaudeAgentOptions, prompt: str) -> SdkResult:
    tools: list[str] = []
    model: str | None = None
    async for message in stream(prompt=prompt, options=options):
        if isinstance(message, sdk.AssistantMessage):
            model = model or message.model
            tools += [b.name for b in message.content if isinstance(b, sdk.ToolUseBlock)]
        elif isinstance(message, sdk.ResultMessage):
            if message.is_error or message.subtype != "success" or message.result is None:
                raise _failure(message.subtype, "; ".join(message.errors or []))
            usage = message.usage or {}
            return SdkResult(message.result, message.total_cost_usd,
                             int(usage.get("input_tokens", 0)), int(usage.get("output_tokens", 0)),
                             model, tuple(tools), message.num_turns)
    raise LLMError("the SDK run ended without a result")


def run_sdk(options: ClaudeAgentOptions, prompt: str, stream: Stream | None = None) -> SdkResult:
    """Run one SDK query to completion and map every failure to a typed error."""
    try:
        return anyio.run(_collect, stream or sdk.query, options, prompt)
    except (LLMError, SpendCapExceeded):
        raise
    except sdk.ResultError as exc:
        raise _failure(getattr(exc, "subtype", None), str(exc)) from exc
    except sdk.ClaudeSDKError as exc:
        raise LLMError(f"{type(exc).__name__}: {exc}"[:300]) from exc


class SdkJudge:
    """The model-judgement port, answered by an SDK run that may use the agent's own tools."""

    def __init__(self, spec: AgentSpec, ctx: ToolContext, broker: ToolBroker, ledger: SpendLedger,
                 config: ClaudeConfig, query: QueryFn = run_sdk, max_turns: int = 8,
                 hint: str = "") -> None:
        self._spec, self._ctx, self._broker, self._ledger = spec, ctx, broker, ledger
        self._config, self._query, self._max_turns, self._hint = config, query, max_turns, hint
        self.results: list[SdkResult] = []

    def _ask(self, task: str, prompt: str, model: str) -> str:
        entry = self._ledger.reserve(task, model)
        try:
            options = agent_options(
                self._spec.name, self._ctx, model=model, max_turns=self._max_turns,
                system_prompt=f"{self._spec.system_prompt}\n{self._hint}\n{_UNTRUSTED_NOTE}",
                invoke=self._broker.invoke, max_budget_usd=self._ledger.budget_for_call())
            result = self._query(options, prompt)
        except SpendCapExceeded:
            self._ledger.fail(entry)
            raise
        except LLMError:
            self._ledger.fail(entry)
            raise
        except Exception as exc:
            self._ledger.fail(entry)
            raise LLMError(f"{type(exc).__name__}: {exc}"[:300]) from exc
        self._ledger.settle(entry, result.cost_usd)
        self.results.append(result)
        if self._broker.tripped:                  # a loop limit fired inside the SDK run
            raise LoopLimitExceeded(self._broker.tripped)
        return result.text

    def classify_reply(self, reply: UntrustedText) -> ReplyClassification:
        text = self._ask("classify_reply", classify_prompt(reply), self._config.model_classify)
        return parse_classification(extract_json_object(text, "classify_reply"))

    def extract_call_fields(self, transcript: UntrustedText, wanted: Sequence[str]) -> CallExtraction:
        text = self._ask("extract_call_fields", extract_prompt(transcript, wanted),
                         self._config.model_drafting)
        return parse_extraction(extract_json_object(text, "extract_call_fields"))

    def draft_email(self, request: DraftRequest) -> DraftProposal:
        text = self._ask("draft_email", draft_prompt(request), self._config.model_drafting)
        return parse_draft(extract_json_object(text, "draft_email"))

    def draft_lesson(self, evidence: UntrustedText) -> LessonDraft:
        text = self._ask("draft_lesson", lesson_prompt(evidence), self._config.model_drafting)
        return parse_lesson(extract_json_object(text, "draft_lesson"))

    def verify_claims(self, draft: Draft, evidence: Evidence) -> Sequence[ClaimVerdict]:
        raise LLMError("claim verification does not run as a tool-using SDK task")


