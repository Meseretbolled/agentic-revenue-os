"""The model, behind one port. Inbound text arrives only as UntrustedText."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from revos.domain.drafting import Claim, Draft, Evidence
from revos.domain.untrusted import UntrustedText

ReplyLabel = Literal["interested", "accept_slot", "not_interested", "question", "price_objection",
                     "unsubscribe", "referral", "out_of_office"]
REPLY_LABELS: tuple[str, ...] = ReplyLabel.__args__  # type: ignore[attr-defined]


@dataclass(frozen=True)
class ReplyClassification:
    label: ReplyLabel
    confidence: float
    rationale: str
    quote: str = ""            # the words in the reply that justify the label
    quote_verified: bool = False       # code checked that the quote really is in the reply


@dataclass(frozen=True)
class ExtractedField:
    value: str
    quote: str                   # verbatim from the transcript; code checks it is really there


@dataclass(frozen=True)
class CallExtraction:
    fields: Mapping[str, ExtractedField]


@dataclass(frozen=True)
class DraftRequest:
    subject_template: str
    body_template: str
    context: Mapping[str, Any]           # company, first_name, sender_name, unsubscribe_line, ...
    evidence: Evidence
    feedback: tuple[str, ...] = ()       # issues from the previous attempt
    purpose: str = "first_touch"         # first_touch | follow_up | reply_ack: what the line is for
    tone: tuple[str, ...] = ()           # the pack's tone rules
    lessons: tuple[Mapping[str, Any], ...] = ()   # cited lessons: {"id", "text"}; an angle, never a fact
    reply: UntrustedText | None = None   # the prospect's reply, fenced as untrusted data


@dataclass(frozen=True)
class DraftProposal:
    """The model writes only the personalised line; the rest of the email is approved text."""

    personalised_line: str
    claims: tuple[Claim, ...]


@dataclass(frozen=True)
class ClaimVerdict:
    claim_index: int
    supported: bool
    reason: str = ""


@dataclass(frozen=True)
class LessonDraft:
    text: str
    tags: tuple[str, ...]


class LLM(Protocol):
    def classify_reply(self, reply: UntrustedText) -> ReplyClassification: ...

    def extract_call_fields(
        self, transcript: UntrustedText, wanted: Sequence[str]
    ) -> CallExtraction: ...

    def draft_email(self, request: DraftRequest) -> DraftProposal: ...

    def verify_claims(self, draft: Draft, evidence: Evidence) -> Sequence[ClaimVerdict]: ...

    def draft_lesson(self, evidence: UntrustedText) -> LessonDraft: ...
