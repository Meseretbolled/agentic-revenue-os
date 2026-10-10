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
            "use_for": study.use_for,
            "figures": study.metric_text, "tags": study.tags, "label": study.label,
            "source_url": study.source_url, "source_verified": study.source_verified}


def list_all(ctx: ToolContext, scope: RunScope, args: dict[str, Any]) -> dict[str, Any]:
    return {"case_studies": [_view(s) for s in _library(ctx, scope)]}


def _already_sent(ctx: ToolContext, scope: RunScope) -> set[str]:
    rows = ctx.conn.execute(
        "SELECT cs.slug FROM case_study_sends s JOIN case_studies cs ON cs.id = s.case_study_id "
        "WHERE s.deal_id = %s", (scope.deal_id,)).fetchall()
    return {r[0] for r in rows}


def pick(ctx: ToolContext, scope: RunScope, args: dict[str, Any]) -> dict[str, Any]:
    """Best approved match for the situation (an objection or a stage), never one this deal already got."""
    tags = {str(t).casefold() for t in args.get("tags", [])}
    words = set(re.findall(r"[a-z0-9]{3,}", str(args.get("query", "")).casefold()))
    situation = str(args.get("situation", "")).casefold()
    sent = _already_sent(ctx, scope)

    def score(study: CaseStudy) -> int:
        text = set(re.findall(r"[a-z0-9]{3,}", f"{study.customer} {study.summary}".casefold()))
        fit = 10 if situation and situation in {u.casefold() for u in study.use_for} else 0
        return fit + 2 * len(tags & {t.casefold() for t in study.tags}) + len(words & text)

    ranked = sorted(((score(s), s) for s in _library(ctx, scope) if s.id not in sent),
                    key=lambda p: (-p[0], p[1].id))
    if situation and ranked and ranked[0][0] < 10:        # a situation needs a study made for it
        ranked = []
    if not ranked or ranked[0][0] == 0:
        return {"case_study": None, "reason": "nothing unsent in the library fits"}
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
             {"tags": {"type": "array", "items": {"type": "string"}}, "query": {"type": "string"},
              "situation": {"type": "string"}}, (), pick),
    ToolSpec("casestudies", "get", "Read one approved case study by id.",
             {"case_study_id": {"type": "string"}}, ("case_study_id",), get),
]
