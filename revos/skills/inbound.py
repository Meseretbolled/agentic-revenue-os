"""Handling inbound text: reply triage and call-field extraction. Quarantined text never reaches
the model; a person sees it instead."""

from dataclasses import dataclass
from typing import Literal

from revos.domain.errors import LLMError, QuarantinedContent, SpendCapExceeded
from revos.domain.injection import Finding
from revos.domain.untrusted import UntrustedText
from revos.ports.llm import LLM, ExtractedField, ReplyClassification

CONFIDENCE_FLOOR = 0.5


@dataclass(frozen=True)
class Triage:
    route: Literal["classified", "quarantine", "human"]
    classification: ReplyClassification | None
    findings: tuple[Finding, ...]
    reason: str

    @property
    def needs_human(self) -> bool:
        return self.route != "classified"


def triage_reply(llm: LLM, reply: UntrustedText) -> Triage:
    if reply.flagged:       # not sent to the model at all; logged by the caller, shown to a person
        return Triage("quarantine", None, reply.findings,
                      "reply contains text that tries to instruct the system")
    try:
        result = llm.classify_reply(reply)
    except SpendCapExceeded:
        raise                  # a typed stop: the run ends needs_human with its own reason code
    except LLMError as exc:
        return Triage("human", None, (), f"classifier unavailable: {exc}")
    if result.confidence < CONFIDENCE_FLOOR:
        return Triage("human", result, (), f"low confidence ({result.confidence:.2f})")
    return Triage("classified", result, (), result.rationale)


@dataclass(frozen=True)
class Extraction:
    fields: dict[str, ExtractedField]
    rejected: dict[str, str]         # field -> why it was dropped


def extract_call_fields(llm: LLM, transcript: UntrustedText, wanted: list[str]) -> Extraction:
    """Keep only fields whose quote really is in the transcript; drop anything else."""
    if transcript.flagged:      # checked here too, so a flagged transcript never reaches the model
        raise QuarantinedContent(transcript.findings)
    raw = llm.extract_call_fields(transcript, wanted)
    kept: dict[str, ExtractedField] = {}
    rejected: dict[str, str] = {}
    for name, item in raw.fields.items():
        if name not in wanted:
            rejected[name] = "not a requested field"
        elif not item.value.strip():
            rejected[name] = "empty value"
        elif not transcript.contains_quote(item.quote):
            rejected[name] = "quote is not in the transcript"
        else:
            kept[name] = item
    return Extraction(kept, rejected)
