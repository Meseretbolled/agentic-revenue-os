"""Claim checking. Deterministic first; the model verifier can only add objections.

Rules, all enforced by code:
* Every number, date and proper noun in the email is extracted by code.
* Outside a declared claim, each must come from approved text (templates, pack wording, case
  studies) or the known names (contact, sender, company). Otherwise the fact is UNDECLARED: fail.
* Inside a declared claim, each number and date must appear in the evidence the claim cites, and
  each name in that evidence or the known names. Approved text elsewhere does not count, so a
  number cannot be smuggled in because it happens to appear in a case study.
* The model verifier must rate every claim as supported. If it is missing, errors, runs out of
  budget or skips a claim, the draft FAILS. Nothing defaults to "supported".

What code cannot catch is meaning (an exaggeration with no new number, name or date). That is the
verifier's job, and why a draft without one cannot pass.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from revos.domain.drafting import Claim, Draft, Evidence, Issue
from revos.domain.errors import LLMError, SpendCapExceeded
from revos.ports.llm import ClaimVerdict

_MONTHS = ("january", "february", "march", "april", "may", "june", "july", "august",
           "september", "october", "november", "december")
_ABBREV = {"jan": "january", "feb": "february", "mar": "march", "apr": "april", "jun": "june",
           "jul": "july", "aug": "august", "sep": "september", "sept": "september",
           "oct": "october", "nov": "november", "dec": "december"}
_WORD_NUMBERS = {w: str(n) for n, w in enumerate(
    "two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen "
    "seventeen eighteen nineteen twenty".split(), start=2)}
_WORD_NUMBERS |= {"thirty": "30", "forty": "40", "fifty": "50", "sixty": "60", "seventy": "70",
                  "eighty": "80", "ninety": "90", "hundred": "100", "thousand": "1000",
                  "million": "1000000", "billion": "1000000000", "dozen": "12"}

_NUMBER = re.compile(r"(?P<cur>[$€£])?\s?(?P<num>\d[\d,]*(?:\.\d+)?)\s?(?P<pct>%|percent\b)?"
                     r"(?P<suffix>[kKmMbB]\b)?")
_MONTH = re.compile(r"\b(?P<m>january|february|march|april|june|july|august|september|october|"
                    r"november|december|May|(?:Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sept?|Oct|Nov|Dec)\.?)\b",
                    re.IGNORECASE)
_WORD = re.compile(r"[^\W\d_][\w’'-]*", re.UNICODE)
_RECENCY = re.compile(r"\b(recent|recently|just|new|newly|latest|currently|right now|yesterday|"
                      r"this (?:week|month|quarter|year)|last (?:week|month|quarter|year)|"
                      r"congrat\w*)\b", re.IGNORECASE)

# Capitalised words that carry no fact. Words that appear in approved text are allowed anyway.
COMMON = frozenset("""hi hello dear hey thanks thank regards best sincerely cheers i i'm i've i'd i'll
we we're we've our you your it it's its the a an and or but so also as at in on of for to with is are
was were do does did no yes ps p.s. would could should can may will happy glad fair one source sources
saw noticed read looking given since this that these those there here if when what how why who which my
me let just following quick question next last today tomorrow monday tuesday wednesday thursday friday
saturday sunday re fwd great sure hope hoping appreciate congrats""".split())

# Words that start a sentence and are capitalised only because they start it (live drafts opened with
# "Running ...", "Per ...", "Across ..."). Only a sentence-initial word followed by a lowercase word can be
# exempt: a name mid-sentence, a multi-word name or an unlisted opener ("Nike saw ...") is still a fact.
OPENERS = frozenset("""per across with without within given since about among over under after before during
between beyond through around against toward towards despite for from into onto upon while whether because
although though many most some each every both several any all few more less another other such
being having making keeping running managing scaling growing building maintaining ensuring""".split())
_SENTENCE_START = re.compile(r"(?:^|[.!?:]\s+|\n\s*)$")

Fact = tuple[str, str]      # (kind, normalised value)


@dataclass(frozen=True)
class Token:
    kind: str                # number | date | name
    key: str
    start: int
    end: int
    raw: str


def _number_key(m: re.Match[str]) -> str:
    num = m.group("num").replace(",", "")
    if "." in num:
        num = num.rstrip("0").rstrip(".")
    num = num.lstrip("0") or "0"
    return ((m.group("cur") or "") + num + (m.group("suffix") or "").lower()
            + ("%" if m.group("pct") else ""))


def _ordinary_opener(text: str, m: re.Match[str], low: str) -> bool:
    """A capitalised word that only starts a sentence: a listed opener or an -ing word, then a lowercase word
    or a number."""
    if not _SENTENCE_START.search(text[:m.start()]):
        return False
    following = text[m.end():m.end() + 40].lstrip()
    if not (following[:1].islower() or following[:1].isdigit()):
        return False                                    # "Running Shoes Inc" or a lone word: keep checking
    return low in OPENERS or (low.endswith("ing") and len(low) > 5)


def extract_tokens(text: str) -> list[Token]:
    """Every number, date and proper noun in `text`, found by code."""
    tokens: list[Token] = []
    covered: list[tuple[int, int]] = []
    for m in _NUMBER.finditer(text):
        tokens.append(Token("number", _number_key(m), m.start(), m.end(), m.group(0).strip()))
        covered.append((m.start(), m.end()))
    for m in _MONTH.finditer(text):
        raw = m.group("m")
        if raw.lower() == "may" and raw != "May":
            continue
        month = raw.lower().rstrip(".")
        tokens.append(Token("date", _ABBREV.get(month, month), m.start(), m.end(), raw))
        covered.append((m.start(), m.end()))
    for m in _WORD.finditer(text):
        word = m.group(0)
        low = re.sub(r"['’]s$", "", word.casefold())
        if low in _WORD_NUMBERS:
            tokens.append(Token("number", _WORD_NUMBERS[low], m.start(), m.end(), word))
        elif "-" in low and all(p in _WORD_NUMBERS for p in low.split("-")):
            tokens.append(Token("number", str(sum(int(_WORD_NUMBERS[p]) for p in low.split("-"))),
                                m.start(), m.end(), word))
        elif word[0].isupper() and low not in COMMON and low not in _MONTHS \
                and not _ordinary_opener(text, m, low) \
                and not any(s <= m.start() < e for s, e in covered):
            tokens.append(Token("name", low, m.start(), m.end(), word))
    return sorted(tokens, key=lambda t: t.start)


def facts_in(*texts: str) -> set[Fact]:
    return {(t.kind, t.key) for text in texts for t in extract_tokens(text)}


class ClaimVerifier(Protocol):
    def verify_claims(self, draft: Draft, evidence: Evidence) -> Sequence[ClaimVerdict]: ...


@dataclass(frozen=True)
class CheckResult:
    passed: bool
    issues: tuple[Issue, ...]
    verifier: str            # ok | missing | error | over_budget | skipped
    error: LLMError | None = None      # the typed error, when the verifier raised one


def _spans(text: str, claim: Claim) -> list[tuple[int, int]]:
    needle = " ".join(claim.text.split())
    spans, start = [], text.find(needle) if needle else -1
    while start != -1:
        spans.append((start, start + len(needle)))
        start = text.find(needle, start + 1)
    return spans


def deterministic_check(
    draft: Draft,
    evidence: Evidence,
    approved_texts: Sequence[str],
    allowed_names: Sequence[str],
) -> list[Issue]:
    issues: list[Issue] = []
    # whitespace is normalised inside each line; line breaks stay, so a paragraph start is a sentence start
    text = "\n".join(" ".join(line.split()) for line in f"{draft.subject}\n{draft.body}".splitlines())
    approved = facts_in(*approved_texts, *allowed_names)
    known_names = {t.key for n in allowed_names for t in extract_tokens(n) if t.kind == "name"}
    known_names |= {w.casefold() for n in allowed_names for w in _WORD.findall(n)}

    spans: list[tuple[int, int, Claim]] = []
    claim_support: dict[int, set[Fact]] = {}
    for i, claim in enumerate(draft.claims):
        found = _spans(text, claim)
        if not found:
            issues.append(Issue("claim_not_in_text", "a declared claim does not appear word for "
                                f"word in the email: '{claim.text[:90]}'", claim.text))
        spans += [(s, e, claim) for s, e in found]
        if not claim.evidence:
            issues.append(Issue("claim_no_evidence", f"claim cites no evidence: '{claim.text[:90]}'",
                                claim.text))
            claim_support[i] = set()
            continue
        unknown = [e for e in claim.evidence if e not in evidence.items]
        if unknown:
            issues.append(Issue("claim_unknown_evidence", "claim cites evidence that does not exist: "
                                + ", ".join(unknown), claim.text))
        cited = [evidence.items[e] for e in claim.evidence if e in evidence.items]
        claim_support[i] = facts_in(*(c.text for c in cited))
        if cited and _RECENCY.search(claim.text) and all(c.stale for c in cited):
            issues.append(Issue("stale_as_recent", "claim calls an old signal recent: "
                                f"'{claim.text[:90]}'", claim.text))

    index = {id(c): i for i, c in enumerate(draft.claims)}
    for token in extract_tokens(text):
        covering = [c for s, e, c in spans if s <= token.start and token.end <= e]
        fact = (token.kind, token.key)
        if covering:
            ok = any(fact in claim_support[index[id(c)]]
                     or (token.kind == "name" and token.key in known_names) for c in covering)
            if not ok:
                issues.append(Issue("unsupported_in_claim", f"'{token.raw}' is in a claim but not "
                                    "in the evidence it cites", token.raw))
        elif fact not in approved and not (token.kind == "name" and token.key in known_names):
            issues.append(Issue("undeclared_fact", f"'{token.raw}' is stated but no declared claim "
                                "covers it and it is not approved text", token.raw))
    return issues


def check_draft(
    draft: Draft,
    evidence: Evidence,
    approved_texts: Sequence[str],
    allowed_names: Sequence[str],
    verifier: ClaimVerifier | None,
) -> CheckResult:
    issues = deterministic_check(draft, evidence, approved_texts, allowed_names)
    if issues:
        return CheckResult(False, tuple(issues), "skipped")     # no spend on a draft that fails
    if not draft.claims:
        return CheckResult(True, (), "skipped")                  # nothing personal to verify
    if verifier is None:
        return CheckResult(False, (Issue("verifier_missing", "no claim verifier is configured"),),
                           "missing")
    try:
        verdicts = list(verifier.verify_claims(draft, evidence))
    except SpendCapExceeded as exc:
        return CheckResult(False, (Issue("verifier_over_budget", f"verifier blocked by the spend "
                                         f"cap: {exc}"),), "over_budget", exc)
    except Exception as exc:  # noqa: BLE001 - any verifier failure fails closed
        return CheckResult(False, (Issue("verifier_error", f"verifier failed: "
                                         f"{type(exc).__name__}: {exc}"),), "error",
                           exc if isinstance(exc, LLMError) else None)

    by_index = {v.claim_index: v for v in verdicts}
    problems = []
    for i, claim in enumerate(draft.claims):
        verdict = by_index.get(i)
        if verdict is None:
            problems.append(Issue("verifier_error", f"verifier gave no verdict for claim {i}: "
                                  f"'{claim.text[:90]}'", claim.text))
        elif not verdict.supported:
            problems.append(Issue("verifier_rejected", f"verifier says this claim is not supported: "
                                  f"'{claim.text[:90]}' ({verdict.reason})", claim.text))
    return CheckResult(not problems, tuple(problems), "ok" if not problems else "rejected")
