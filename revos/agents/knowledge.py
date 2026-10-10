import re

from revos.agents.common import make
from revos.agents.types import AgentEnv, AgentOutput
from revos.domain.errors import QuarantinedContent


def _norm(text: str) -> str:
    return re.sub(r"\W+", " ", text.casefold()).strip()


def run(env: AgentEnv) -> AgentOutput:
    """Turn a quoted customer statement into a lesson, unless the same lesson already exists."""
    candidate = env.untrusted["candidate"]
    if candidate.flagged:
        raise QuarantinedContent(candidate.findings)
    draft = env.judge.draft_lesson(candidate)
    found = env.tools.call("lessons.search", {"query": draft.text, "tags": list(draft.tags),
                                              "limit": 5})["lessons"]
    for existing in found:
        if _norm(existing["text"]) == _norm(draft.text):
            return AgentOutput(notes=f"duplicate of lesson L{existing['id']}")
    proposal = make("lesson_write", env.deal_id, env.contact_id, {
        "text": draft.text, "tags": list(draft.tags),
        "evidence": candidate.for_human()["text"], "origin": candidate.origin},
        [], "a new lesson backed by a customer quote")
    return AgentOutput((proposal,))
