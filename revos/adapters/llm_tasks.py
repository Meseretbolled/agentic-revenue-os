"""Prompts and strict parsers for the model tasks, shared by every real model adapter."""

import json
import re
from collections.abc import Sequence
from typing import Any

from revos.domain.drafting import Claim, Draft, Evidence
from revos.domain.errors import LLMError
from revos.domain.untrusted import UntrustedText
from revos.ports.llm import (
    REPLY_LABELS,
    CallExtraction,
    ClaimVerdict,
    DraftProposal,
    DraftRequest,
    ExtractedField,
    LessonDraft,
    ReplyClassification,
)


def extract_json_object(raw: str, task: str) -> dict[str, Any]:
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    try:
        data = json.loads(match.group(0)) if match else None
    except json.JSONDecodeError:
        data = None
    if not isinstance(data, dict):
        raise LLMError(f"{task}: the model did not return a JSON object")
    return data


def classify_prompt(reply: UntrustedText) -> str:
    return (f"Classify this customer reply.\n{reply.for_model()}\n"
            f'Return {{"label": one of {list(REPLY_LABELS)}, "confidence": 0..1, "rationale": short}}')


def parse_classification(data: dict[str, Any]) -> ReplyClassification:
    label, confidence = data.get("label"), data.get("confidence")
    if label not in REPLY_LABELS or not isinstance(confidence, int | float) \
            or isinstance(confidence, bool) or not 0 <= confidence <= 1:
        raise LLMError("classify_reply: label or confidence is invalid")
    return ReplyClassification(label, float(confidence), str(data.get("rationale", ""))[:300])


def extract_prompt(transcript: UntrustedText, wanted: Sequence[str]) -> str:
    return (f"Extract these fields from the call: {list(wanted)}. Give a verbatim quote for each."
            f"\n{transcript.for_model()}\n"
            'Return {"fields": {name: {"value": ..., "quote": ...}}}. Omit fields not stated.')


def parse_extraction(data: dict[str, Any]) -> CallExtraction:
    fields = {}
    for name, item in (data.get("fields") or {}).items():
        if isinstance(item, dict) and isinstance(item.get("value"), str) \
                and isinstance(item.get("quote"), str):
            fields[str(name)] = ExtractedField(item["value"], item["quote"])
    return CallExtraction(fields)


def draft_prompt(request: DraftRequest) -> str:
    evidence = "\n".join(f"{i.id}: {i.text}" + (f" [FICTIONAL DEMO FACT, source: {i.source_url}]"
                                               if i.demo else "")
                         for i in request.evidence.items.values())
    demo_rule = ("\nA fact marked FICTIONAL DEMO FACT must be written with its source label in the "
                 "line and never presented as something you read or researched."
                 if any(i.demo for i in request.evidence.items.values()) else "")
    feedback = ("\nFix these problems from the last attempt:\n- " + "\n- ".join(request.feedback)
                if request.feedback else "")
    angle = (f"\nLead with this angle, as an angle only (never as a new fact): "
             f"{request.context['angle']}" if request.context.get("angle") else "")
    return (f"Write ONE short personalised line for {request.context.get('first_name', 'the contact')} "
            f"at {request.context.get('company', 'their company')}. Use only facts from the evidence "
            f"below; do not add numbers, dates or names that are not in it.{angle}{demo_rule}\n"
            f"Evidence:\n{evidence}{feedback}\n"
            'Return {"personalised_line": str, "claims": [{"text": exact words from the line, '
            '"evidence": [ids]}]}')


def parse_draft(data: dict[str, Any]) -> DraftProposal:
    line, raw_claims = data.get("personalised_line"), data.get("claims")
    if not isinstance(line, str) or not isinstance(raw_claims, list):
        raise LLMError("draft_email: missing personalised_line or claims")
    claims = tuple(Claim(str(c.get("text", "")), tuple(map(str, c.get("evidence", []))))
                   for c in raw_claims if isinstance(c, dict))
    return DraftProposal(line.strip(), claims)


def verify_prompt(draft: Draft, evidence: Evidence) -> str:
    claims = "\n".join(f"{i}: {c.text} (cites {', '.join(c.evidence)})"
                       for i, c in enumerate(draft.claims))
    facts = "\n".join(f"{i.id}: {i.text}" for i in evidence.items.values())
    return (f"Is each claim fully supported by the evidence it cites? Judge meaning: exaggeration, "
            f"wrong roles, wrong timing.\nEvidence:\n{facts}\nClaims:\n{claims}\n"
            'Return {"verdicts": [{"claim_index": int, "supported": bool, "reason": str}]}')


def parse_verdicts(data: dict[str, Any]) -> list[ClaimVerdict]:
    out = []
    for v in data.get("verdicts") or []:
        if isinstance(v, dict) and isinstance(v.get("claim_index"), int) \
                and isinstance(v.get("supported"), bool):
            out.append(ClaimVerdict(v["claim_index"], v["supported"], str(v.get("reason", ""))[:300]))
    return out


def lesson_prompt(evidence: UntrustedText) -> str:
    return (f"Turn this customer statement into one short, reusable sales lesson.\n"
            f"{evidence.for_model()}\n"
            'Return {"text": the lesson, "tags": [short lowercase tags]}')


def parse_lesson(data: dict[str, Any]) -> LessonDraft:
    text, tags = data.get("text"), data.get("tags")
    if not isinstance(text, str) or not text.strip() or not isinstance(tags, list):
        raise LLMError("draft_lesson: missing text or tags")
    return LessonDraft(text.strip()[:300], tuple(str(t).casefold() for t in tags)[:6])
