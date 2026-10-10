from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal
from uuid import UUID

from revos.domain.stages import DealStage
from revos.domain.untrusted import UntrustedText
from revos.ports.calendar import Slot
from revos.ports.clock import Clock
from revos.ports.llm import LLM
from revos.segments.loader import Pack


@dataclass(frozen=True)
class Target:
    deal_id: UUID
    contact_id: UUID | None = None


@dataclass(frozen=True)
class Proposal:
    """What an agent wants done. It is a request: only the orchestrator turns it into anything,
    and only through the policy engine, the send gate and the state machine."""

    action_type: str
    target: Target
    payload: Mapping[str, Any]              # JSON-friendly
    evidence_ids: tuple[str, ...]
    reason: str


@dataclass(frozen=True)
class AgentOutput:
    proposals: tuple[Proposal, ...] = ()
    stage_suggestion: DealStage | None = None      # a hint; the state machine still decides
    needs_human: str | None = None                 # set when the agent cannot continue by itself
    notes: str = ""


@dataclass(frozen=True)
class Identity:
    """Who is writing: filled into every email."""

    sender_name: str = "Jo Rivers"
    unsubscribe_line: str = "To stop receiving these emails, reply STOP."
    postal_address: str = "1 Example Street, Sampletown"


@dataclass
class AgentEnv:
    """Everything an agent procedure may use. Note what is absent: no mailer, no CRM, no e-sign,
    no database writer. Reading goes through the scoped tool broker."""

    agent: str
    step: str
    runner: str
    deal_id: UUID
    contact_id: UUID | None
    company_id: UUID | None
    pack: Pack
    tools: Any                                   # ToolBroker
    clock: Clock
    identity: Identity
    input: Mapping[str, Any] = field(default_factory=dict)             # trusted, structured
    untrusted: Mapping[str, UntrustedText] = field(default_factory=dict)  # inbound text, fenced
    slots: tuple[Slot, ...] = ()
    judge: LLM | None = None                     # model judgement (drafting, classifying, ...)
    verifier: LLM | None = None                  # claim verification


@dataclass(frozen=True)
class AgentSpec:
    name: str
    kind: Literal["deterministic", "judgement"]
    description: str
    run: Callable[[AgentEnv], AgentOutput]
    system_prompt: str = ""
