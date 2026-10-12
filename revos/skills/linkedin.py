"""LinkedIn DRAFTS: what a draft is made of and how it is checked. Nothing here talks to LinkedIn, and
nothing in the system can: a person copies the text and sends it. The wording, the length limits and
whether a case study is cited come from the pack's `linkedin` section (messaging.yaml), never from code."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import jinja2

from revos.domain.drafting import Claim, Draft, Evidence, Issue
from revos.ports.llm import DraftRequest
from revos.segments.loader import Pack
from revos.segments.schema import CaseStudy, LinkedInConfig
from revos.skills.claims import deterministic_check
from revos.skills.drafting import model_text_issues

PIECES = ("note", "message")


@dataclass(frozen=True)
class Piece:
    kind: str  # note | message
    template: str
    fallback: str
    max_chars: int
    expected_asks: int  # the note asks nothing; the follow-up message holds the one ask
    purpose: str


def pieces(cfg: LinkedInConfig) -> list[Piece]:
    return [
        Piece("note", cfg.note_template, cfg.note_fallback, cfg.note_max_chars, 0, "linkedin_note"),
        Piece(
            "message",
            cfg.message_template,
            cfg.message_fallback,
            cfg.message_max_chars,
            1,
            "linkedin_message",
        ),
    ]


DEMO_LABEL = "[Fictional demo profile] "


def build_context(
    company: str, contact_name: str | None, sender: str, study: CaseStudy | None, evidence: Evidence
) -> dict[str, Any]:
    """What the pack's LinkedIn wording may use. The demo label is added by code whenever the lead's facts
    come from a fictional profile, so the label never depends on the model remembering it."""
    first = (contact_name or "").split()[0] if (contact_name or "").strip() else "there"
    demo = any(item.demo for item in evidence.items.values())
    return {
        "company": company,
        "first_name": first,
        "sender_name": sender,
        "case_study": study,
        "demo_label": DEMO_LABEL if demo else "",
    }


def render(template: str, context: Mapping[str, Any], line: str) -> str:
    env = jinja2.Environment(undefined=jinja2.StrictUndefined, autoescape=False)
    return env.from_string(template).render({**context, "personalised_line": line}).strip()


def line_budget(piece: Piece, context: Mapping[str, Any]) -> int:
    """How long the model's line may be: the limit minus the pack's own wording around it."""
    return max(0, piece.max_chars - len(render(piece.template, context, "")))


def request_for(
    piece: Piece,
    cfg: LinkedInConfig,
    context: Mapping[str, Any],
    evidence: Evidence,
    feedback: tuple[str, ...] = (),
) -> DraftRequest:
    ctx = {**context, "line_budget": line_budget(piece, context)}
    return DraftRequest(
        "",
        piece.template,
        ctx,
        evidence,
        feedback=feedback,
        purpose=piece.purpose,
        tone=tuple(cfg.tone),
        max_chars=piece.max_chars,
        expected_asks=piece.expected_asks,
        label_demo_facts=True,
    )


def inputs(
    pack: Pack, sender: str, company: str, contact_name: str | None, study: CaseStudy | None
) -> tuple[list[str], list[str]]:
    """(approved texts, known names) for the claim checker: the pack's own LinkedIn wording, its pillars and
    snippets, and the one case study's stored facts. Shared by the agent and the orchestrator's re-check."""
    cfg = pack.messaging.linkedin
    assert cfg is not None
    approved = [
        DEMO_LABEL,
        cfg.note_template,
        cfg.message_template,
        cfg.note_fallback,
        cfg.message_fallback,
        *(f"{p.headline} {p.body}" for p in pack.messaging.pillars),
        *pack.messaging.snippets.values(),
    ]
    names = [company, contact_name or "", sender]
    if study:
        approved += [study.customer, study.summary, study.metric_text, study.source_url, study.label]
        names.append(study.customer)
    return approved, [n for n in names if n]


def recheck(
    pack: Pack,
    sender: str,
    company: str,
    contact_name: str | None,
    study: CaseStudy | None,
    evidence: Evidence,
    context: Mapping[str, Any],
    payload: Mapping[str, Any],
) -> list[Issue]:
    """Run the checks again from what the DATABASE holds (its facts, the pack's wording), per stored piece:
    every fact against its cited evidence, the length, the number of asks, the fictional-profile label.
    The agent's own claim of having passed is never taken on trust."""
    cfg = pack.messaging.linkedin
    if cfg is None:
        return [Issue("no_linkedin_section", "the pack has no linkedin section", None)]
    approved, names = inputs(pack, sender, company, contact_name, study)
    issues: list[Issue] = []
    stored: Mapping[str, Any] = payload.get("pieces") or {}
    for piece in pieces(cfg):
        item = stored.get(piece.kind) or {}
        claims: Sequence[Claim] = tuple(
            Claim(c["text"], tuple(c["evidence"])) for c in item.get("claims", [])
        )
        draft = Draft("", str(item.get("text", "")), claims)
        if not draft.body:
            issues.append(Issue("piece_missing", f"the {piece.kind} is missing", None))
            continue
        issues += deterministic_check(draft, evidence, approved, names)
        issues += model_text_issues(draft, request_for(piece, cfg, context, evidence), "")
    return issues
