"""Small helpers the agents share."""

import re
from collections.abc import Mapping, Sequence
from typing import Any
from uuid import UUID

from revos.agents.types import Identity, Proposal, Target
from revos.domain.drafting import Evidence, EvidenceItem
from revos.segments.loader import Pack
from revos.segments.schema import CaseStudy, EmailTemplate


def unfence(block: str) -> str:
    """The text inside an UNTRUSTED block (tool results arrive fenced)."""
    lines = block.splitlines()
    if len(lines) >= 2 and lines[0].startswith("<<UNTRUSTED") and lines[-1].startswith("<</UNTRUSTED"):
        return "\n".join(lines[1:-1])
    return block


def clean_fields(fields: Mapping[str, Any]) -> dict[str, Any]:
    """Deal fields as plain values: fenced strings are unwrapped, other types pass through."""
    return {k: unfence(v) if isinstance(v, str) else v for k, v in fields.items()}


def lead_facts(fields: Mapping[str, Any], pack: Pack) -> dict[str, Any]:
    return {k: v for k, v in clean_fields(fields).items() if k in pack.scoring.lead_fields}


DEMO_FACT_PREFIX = "DEMO"          # fact ids that come from a fictional demo profile, never from a page
DEMO_SOURCE_PREFIX = "fictional demo profile"
WEB_FACT_PREFIX = "W"          # facts from real web research (see agents/enrichment.py)


def evidence_from_fields(fields: Mapping[str, Any]) -> Evidence:
    """Researched facts are stored as fact:<id> (the sentence) and fact_source:<id> (the page)."""
    plain = clean_fields(fields)

    def citable(fact_id: str) -> bool:
        """A fact from real web research can be cited only with a stored source URL and fetch time."""
        if not fact_id.startswith(WEB_FACT_PREFIX) or fact_id.startswith(DEMO_FACT_PREFIX):
            return True
        return str(plain.get(f"fact_source:{fact_id}", "")).startswith(("http://", "https://")) \
            and bool(plain.get(f"fact_fetched:{fact_id}"))

    ids = sorted((k.split(":", 1)[1] for k in plain if k.startswith("fact:") and citable(k.split(":", 1)[1])),
                 key=lambda i: (i.startswith(DEMO_FACT_PREFIX), len(i), i))    # real research comes first
    return Evidence.of(EvidenceItem(i, plain[f"fact:{i}"], plain.get(f"fact_source:{i}"),
                                    demo=i.startswith(DEMO_FACT_PREFIX)) for i in ids)


def match_tags(fields: Mapping[str, Any]) -> list[str]:
    """Tags for picking case studies and lessons: the industry and any true yes/no field."""
    plain = clean_fields(fields)
    tags = [str(plain["industry"])] if plain.get("industry") else []
    tags += [name.removeprefix("is_") for name, v in plain.items() if v is True]
    count = plain.get("location_count")
    if isinstance(count, int | float) and not isinstance(count, bool) and count >= 2 \
            and "multi_location" not in tags:
        tags.append("multi_location")
    said = " ".join(str(plain.get(k, "")) for k in ("needs", "objections", "competitors")).casefold()
    if any(w in said for w in ("incremental", "attribution", "net-new", "which campaigns")):
        tags += [t for t in ("trust", "incrementality") if t not in tags]
    return tags


def first_name(full_name: str | None) -> str:
    return (full_name or "").split()[0] if (full_name or "").strip() else "there"


def check_inputs(pack: Pack, template: EmailTemplate, identity: Identity, company: str,
                 contact_name: str | None, case_study: CaseStudy | None,
                 extra_texts: Sequence[str] = ()) -> tuple[list[str], list[str]]:
    """(approved texts, known names) for the claim checker. Shared by the agent that drafts and
    by the orchestrator that re-checks, so both judge against the same inputs."""
    approved = [template.subject, template.body,
                *(f"{p.headline} {p.body}" for p in pack.messaging.pillars),
                *pack.messaging.snippets.values(), *extra_texts]
    names = [company, contact_name or "", identity.sender_name, identity.unsubscribe_line,
             identity.postal_address]
    if case_study:
        approved += [case_study.customer, case_study.summary, case_study.metric_text,
                     case_study.source_url, case_study.label]
        names.append(case_study.customer)
    return approved, [n for n in names if n]


def make(action_type: str, deal_id: UUID, contact_id: UUID | None, payload: Mapping[str, Any],
         evidence: Sequence[str], reason: str) -> Proposal:
    return Proposal(action_type, Target(deal_id, contact_id), dict(payload), tuple(evidence), reason)


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.casefold()).strip("_")


def lesson_query(fields: Mapping[str, Any]) -> tuple[str, list[str]]:
    """(keywords, tags) to retrieve lessons by: the industry, the needs and objections on record."""
    plain = clean_fields(fields)
    parts = [str(plain.get(k)) for k in ("industry", "needs", "objections") if plain.get(k)]
    parts += [str(x).replace("_", " ") for x in plain.get("open_objections", [])]
    return " ".join(parts), match_tags(fields)
