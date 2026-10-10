import re
from typing import Any

from revos.mcp.registry import ToolContext, ToolSpec
from revos.mcp.scope import RunScope, ToolBlocked
from revos.segments.schema import CaseStudy


def _library(ctx: ToolContext, scope: RunScope) -> tuple[CaseStudy, ...]:
    pack = ctx.packs.get(scope.segment_id)
    if pack is None:
        raise ToolBlocked("no case-study library exists for this run's segment")
    return pack.case_studies


def _view(study: CaseStudy) -> dict[str, Any]:
    return {"id": study.id, "customer": study.customer, "summary": study.summary,
            "figures": study.metric_text, "tags": study.tags, "label": study.label,
            "source_url": study.source_url, "source_verified": study.source_verified}


def list_all(ctx: ToolContext, scope: RunScope, args: dict[str, Any]) -> dict[str, Any]:
    return {"case_studies": [_view(s) for s in _library(ctx, scope)]}


def pick(ctx: ToolContext, scope: RunScope, args: dict[str, Any]) -> dict[str, Any]:
    tags = {str(t).casefold() for t in args.get("tags", [])}
    words = set(re.findall(r"[a-z0-9]{3,}", str(args.get("query", "")).casefold()))

    def score(study: CaseStudy) -> int:
        text = set(re.findall(r"[a-z0-9]{3,}", f"{study.customer} {study.summary}".casefold()))
        return 2 * len(tags & {t.casefold() for t in study.tags}) + len(words & text)

    ranked = sorted(((score(s), s) for s in _library(ctx, scope)), key=lambda p: (-p[0], p[1].id))
    if not ranked or ranked[0][0] == 0:
        return {"case_study": None, "reason": "nothing in the library matches"}
    return {"case_study": _view(ranked[0][1]), "match_score": ranked[0][0]}


def get(ctx: ToolContext, scope: RunScope, args: dict[str, Any]) -> dict[str, Any]:
    wanted = str(args.get("case_study_id", ""))
    for study in _library(ctx, scope):
        if study.id == wanted:
            return _view(study)
    raise ToolBlocked("case_study_id is not in this segment's library")


SPECS = [
    ToolSpec("casestudies", "list", "List this segment's approved case studies.", {}, (), list_all),
    ToolSpec("casestudies", "pick", "Pick the best-matching approved case study.",
             {"tags": {"type": "array", "items": {"type": "string"}}, "query": {"type": "string"}},
             (), pick),
    ToolSpec("casestudies", "get", "Read one approved case study by id.",
             {"case_study_id": {"type": "string"}}, ("case_study_id",), get),
]
