"""Draft an email: the model writes the personalised line, approved templates write the rest."""

from collections.abc import Sequence
from dataclasses import dataclass, replace

import jinja2

from revos.domain.drafting import Draft, Issue
from revos.domain.errors import LLMError
from revos.ports.llm import LLM, DraftProposal, DraftRequest
from revos.skills.claims import ClaimVerifier, check_draft

MAX_REGENERATIONS = 2


@dataclass(frozen=True)
class DraftOutcome:
    status: str                       # passed | needs_human
    draft: Draft | None
    attempts: int
    issues: tuple[Issue, ...]


def render_email(request: DraftRequest, proposal: DraftProposal) -> Draft:
    env = jinja2.Environment(undefined=jinja2.StrictUndefined, autoescape=False)
    context = {**request.context, "personalised_line": proposal.personalised_line}
    try:
        subject = env.from_string(request.subject_template).render(context)
        body = env.from_string(request.body_template).render(context)
    except jinja2.UndefinedError as exc:
        raise LLMError(f"cannot render the email: {exc}") from exc
    return Draft(" ".join(subject.split()), body.strip(), tuple(proposal.claims))


def draft_with_checks(
    llm: LLM,
    request: DraftRequest,
    approved_texts: Sequence[str],
    allowed_names: Sequence[str],
    verifier: ClaimVerifier | None,
    *,
    max_regenerations: int = MAX_REGENERATIONS,
) -> DraftOutcome:
    """Draft, check, and regenerate at most `max_regenerations` times with the issues as feedback.

    A draft that still fails is handed to a person as it is. No sentence is ever cut out of it.
    """
    attempts = 0
    current = request
    while True:
        attempts += 1
        try:
            proposal = llm.draft_email(current)
            draft = render_email(current, proposal)
        except LLMError as exc:
            return DraftOutcome("needs_human", None, attempts,
                                (Issue("llm_unavailable", f"{type(exc).__name__}: {exc}"),))
        result = check_draft(draft, request.evidence, approved_texts, allowed_names, verifier)
        if result.passed:
            return DraftOutcome("passed", draft, attempts, ())
        retryable = not any(i.infrastructure for i in result.issues)
        if not retryable or attempts > max_regenerations:
            return DraftOutcome("needs_human", draft, attempts, result.issues)
        current = replace(request, feedback=tuple(i.message for i in result.issues))
