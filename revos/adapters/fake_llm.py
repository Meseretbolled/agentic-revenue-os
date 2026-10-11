"""Deterministic stand-in for the model, so the whole system runs offline with no key.

Same inputs always give the same outputs. It is rule based and deliberately simple: it proves the
plumbing and the safety checks, not language understanding. Like the real adapter it can only see
inbound text through UntrustedText.for_model(), so quarantined text cannot reach it.
"""

import re
from collections import deque
from collections.abc import Sequence

from revos.domain.drafting import Draft, Evidence
from revos.domain.quoted import new_text, quote_around
from revos.domain.untrusted import UntrustedText
from revos.ports.llm import (
    CallExtraction,
    ClaimVerdict,
    DraftProposal,
    DraftRequest,
    ExtractedField,
    LessonDraft,
    ReplyClassification,
)

_REPLY_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("unsubscribe", ("unsubscribe", "remove me", "stop emailing", "take me off", "abmelden",
                     "désabonner", "darme de baja")),
    ("out_of_office", ("out of office", "automatic reply", "auto-reply", "on vacation",
                       "on leave until")),
    ("referral", ("talk to my colleague", "reach out to", "better person", "forwarded your email",
                  "cc'ing")),
    ("price_objection", ("too expensive", "pricing", "price", "cost", "budget", "discount",
                         "cheaper")),
    ("not_interested", ("not interested", "no thanks", "no thank you", "not a fit",
                        "we're all set")),
    ("accept_slot", ("works for me", "that works", "i can do", "let's do", "lets do", "i'll take",
                     "book me")),
    ("interested", ("interested", "sounds good", "let's talk", "lets talk", "send me more",
                    "book a call", "happy to chat")),
    ("question", ("?",)),
)

_CALL_RULES: dict[str, tuple[str, ...]] = {
    "budget": ("budget",),
    "timeline": ("timeline", "by q", "weeks", "months", "launch", "deadline"),
    "decision_maker": ("decision maker", "decision-maker", "approve this"),
    "signer": ("will sign", "is the signer", "signs the", "signatory"),
    "needs": ("we need", "looking for", "need to", "challenge"),
    "objections": ("concern", "worried", "too expensive", "not sure"),
}


_LESSON_TAGS: dict[str, tuple[str, ...]] = {
    "pricing": ("price", "pricing", "cost", "expensive", "budget"),
    "compliance": ("compliance", "brand"),
    "timing": ("timeline", "launch", "weeks", "quarter"),
    "attribution": ("attribution", "which campaigns", "cannot tell", "measure"),
    "objection": ("concern", "worried", "not sure"),
}


class FakeLLM:
    """`script` lets a test queue exact draft proposals; `fail` makes a method raise."""

    def __init__(
        self,
        script: Sequence[DraftProposal] = (),
        fail: dict[str, Exception] | None = None,
        verdicts: Sequence[ClaimVerdict] | None = None,
    ) -> None:
        self._script = deque(script)
        self._fail = fail or {}
        self._verdicts = verdicts
        self.calls: list[str] = []
        self.prompts: list[str] = []          # every piece of inbound text that reached the "model"
        self.feedback_seen: list[tuple[str, ...]] = []

    def _enter(self, method: str) -> None:
        self.calls.append(method)
        if method in self._fail:
            raise self._fail[method]

    def classify_reply(self, reply: UntrustedText) -> ReplyClassification:
        self._enter("classify_reply")
        block = reply.for_model()
        self.prompts.append(block)
        fresh = new_text(block)               # never classify our own quoted words
        text = fresh.casefold()
        for label, needles in _REPLY_RULES:
            hit = next((n for n in needles if n in text), None)
            if hit:
                return ReplyClassification(label, 0.9, f"matched '{hit}'",  # type: ignore[arg-type]
                                           quote_around(fresh, hit))
        return ReplyClassification("question", 0.3, "no rule matched")

    def extract_call_fields(self, transcript: UntrustedText, wanted: Sequence[str]) -> CallExtraction:
        self._enter("extract_call_fields")
        block = transcript.for_model()
        self.prompts.append(block)
        sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", block) if s.strip()]
        fields: dict[str, ExtractedField] = {}
        for name in wanted:
            needles = _CALL_RULES.get(name, (name.replace("_", " "),))
            for sentence in sentences:
                if (any(n in sentence.casefold() for n in needles) and "UNTRUSTED" not in sentence
                        and not sentence.endswith("?")):          # a question states no fact
                    quote = re.sub(r"^[\w .'-]{1,30}:\s*", "", sentence)      # drop "Speaker:" prefix
                    fields[name] = ExtractedField(value=quote, quote=quote)
                    break
        return CallExtraction(fields)

    def draft_email(self, request: DraftRequest) -> DraftProposal:
        self._enter("draft_email")
        self.feedback_seen.append(request.feedback)
        if self._script:
            return self._script.popleft()
        items = list(request.evidence.items.values())
        if not items:
            return DraftProposal("I wanted to reach out about local campaigns.", ())
        first = items[0]
        line = f"I noticed that {first.text}"
        return DraftProposal(line, (_claim(line, first.id),))

    def verify_claims(self, draft: Draft, evidence: Evidence) -> Sequence[ClaimVerdict]:
        self._enter("verify_claims")
        if self._verdicts is not None:
            return list(self._verdicts)
        verdicts = []
        for i, claim in enumerate(draft.claims):
            cited = [evidence.items[e].text.casefold() for e in claim.evidence if e in evidence.items]
            text = claim.text.casefold()
            ok = bool(cited) and all(c in text or text in c for c in cited)
            verdicts.append(ClaimVerdict(i, ok, "restates its evidence" if ok
                                         else "does not restate its cited evidence"))
        return verdicts


    def draft_lesson(self, evidence: UntrustedText) -> LessonDraft:
        self._enter("draft_lesson")
        block = evidence.for_model()
        self.prompts.append(block)
        quote = " ".join(line for line in block.splitlines()[1:-1]).strip()
        tags = tuple(t for t, words in _LESSON_TAGS.items() if any(w in quote.casefold() for w in words))
        return LessonDraft(f"Prospects said: {quote}"[:300], tags)


def _claim(text: str, evidence_id: str):
    from revos.domain.drafting import Claim
    return Claim(text, (evidence_id,))
