"""Draft an email: the model writes the personalised line, approved templates write the rest."""

from collections.abc import Sequence
from dataclasses import dataclass, replace

import jinja2

from revos.domain.drafting import Draft, Issue
from revos.domain.errors import LLMError
from revos.domain.injection import scan
from revos.ports.llm import LLM, DraftProposal, DraftRequest
from revos.skills.claims import ClaimVerifier, check_draft

MAX_REGENERATIONS = 2


@dataclass(frozen=True)
class DraftOutcome:
    status: str                       # passed | needs_human
    draft: Draft | None
    attempts: int
    issues: tuple[Issue, ...]
    error: LLMError | None = None     # the typed model error that stopped the draft, if any
    fallback: tuple[str, ...] = ()    # the issue codes that sent us to the pack's template wording


def render_email(request: DraftRequest, proposal: DraftProposal) -> Draft:
    env = jinja2.Environment(undefined=jinja2.StrictUndefined, autoescape=False)
    context = {**request.context, "personalised_line": proposal.personalised_line}
    try:
        subject = env.from_string(request.subject_template).render(context)
        body = env.from_string(request.body_template).render(context)
    except jinja2.UndefinedError as exc:
        raise LLMError(f"cannot render the email: {exc}") from exc
    return Draft(" ".join(subject.split()), body.strip(), tuple(proposal.claims))


FOOTER_KEYS = ("unsubscribe_line", "postal_address")


def model_text_issues(draft: Draft, request: DraftRequest, written: str) -> list[Issue]:
    """Checks that only depend on what the model wrote, before any claim is looked at:
    * the written text must not try to instruct anyone (the injection scanner),
    * the finished email must still carry the unsubscribe line and the postal address."""
    issues: list[Issue] = []
    if found := scan(written):
        issues.append(Issue("injection_in_draft", "the written text contains instruction-like wording ("
                            + ", ".join(sorted({f.rule_id for f in found})) + "); write plain sales prose",
                            written))
    for key in FOOTER_KEYS:
        value = str(request.context.get(key) or "").strip()
        if value and value not in draft.body:
            issues.append(Issue("footer_missing", f"the email lost its {key.replace('_', ' ')}", None))
    return issues


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
                                (Issue("llm_unavailable", f"{type(exc).__name__}: {exc}"),), exc)
        early = model_text_issues(draft, request, proposal.personalised_line)
        if early:
            if attempts > max_regenerations:
                return DraftOutcome("needs_human", draft, attempts, tuple(early))
            current = replace(request, feedback=tuple(i.message for i in early))
            continue
        result = check_draft(draft, request.evidence, approved_texts, allowed_names, verifier)
        if result.passed:
            return DraftOutcome("passed", draft, attempts, ())
        retryable = not any(i.infrastructure for i in result.issues)
        if not retryable or attempts > max_regenerations:
            return DraftOutcome("needs_human", draft, attempts, result.issues, result.error)
        current = replace(request, feedback=tuple(i.message for i in result.issues))


def render_fixed(pack, template_name: str, context) -> Draft:
    """An email made only of a pack template and values we supply. No model writes any of it."""
    template = pack.messaging.templates[template_name]
    env = jinja2.Environment(undefined=jinja2.StrictUndefined, autoescape=False)
    try:
        subject = env.from_string(template.subject).render(context)
        body = env.from_string(template.body).render(context)
    except jinja2.UndefinedError as exc:
        raise LLMError(f"cannot render {template_name}: {exc}") from exc
    return Draft(" ".join(subject.split()), body.strip(), ())


def render_case_study_reply(pack, study, context, original_subject: str) -> Draft:
    """The answer to an objection: the pack's template filled with one case study's STORED facts."""
    return render_fixed(pack, "case_study_reply",
                        {**context, "case_study": study, "original_subject": original_subject})


def draft_with_fallback(llm: LLM, request: DraftRequest, approved_texts: Sequence[str],
                        allowed_names: Sequence[str], verifier: ClaimVerifier | None, fallback_line: str,
                        ) -> DraftOutcome:
    """Model-written text, checked; if a CHECK fails (a claim, the injection scan, the footer), the
    email is rebuilt from the pack's own wording and the outcome says why. Model or budget errors and
    an unavailable verifier are not content failures: they are returned as they are, never papered over."""
    outcome = draft_with_checks(llm, request, approved_texts, allowed_names, verifier)
    if outcome.status == "passed":
        return outcome
    if outcome.error is not None or any(
            i.infrastructure or i.code == "llm_unavailable" for i in outcome.issues):
        return outcome
    codes = tuple(sorted({i.code for i in outcome.issues}))
    plain = render_email(request, DraftProposal(fallback_line, ()))
    result = check_draft(plain, request.evidence, [*approved_texts, fallback_line], allowed_names, None)
    if not result.passed or model_text_issues(plain, request, ""):
        return outcome                        # even the template fails: hand it to a person
    return DraftOutcome("passed", plain, outcome.attempts, (), None, codes)
