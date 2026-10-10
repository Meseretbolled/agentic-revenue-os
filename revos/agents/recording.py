"""RecordingLLM: gives the model port the same record-and-replay behaviour as tools.

A model call that already succeeded in this run is answered from the database on resume, so a
crash never makes the system pay for, or act on, the same judgement twice."""

import json
from collections import defaultdict, deque
from collections.abc import Sequence
from dataclasses import asdict
from typing import Any
from uuid import UUID

from revos.agents.store import RunStore, canonical_hash
from revos.domain.drafting import Claim, Draft, Evidence
from revos.domain.untrusted import UntrustedText
from revos.ports.llm import (
    LLM,
    CallExtraction,
    ClaimVerdict,
    DraftProposal,
    DraftRequest,
    ExtractedField,
    LessonDraft,
    ReplyClassification,
)


class RecordingLLM:
    def __init__(self, inner: LLM, store: RunStore, run_id: UUID) -> None:
        self._inner, self._store, self._run_id = inner, store, run_id
        self.executed = 0
        self.replayed = 0
        self._cache: dict[tuple[str, str], deque[Any]] = defaultdict(deque)
        for row in store.steps(run_id):
            if row["kind"] == "llm_call" and row["ok"]:
                self._cache[(row["name"], row["args_hash"])].append(row["result"])

    def _run(self, name: str, digest_of: Any, call, encode, decode):
        digest = canonical_hash(digest_of)
        queue = self._cache.get((name, digest))
        if queue:
            self.replayed += 1
            return decode(queue.popleft())
        result = call()                                # a typed error propagates unrecorded
        self._store.record_step(self._run_id, "llm_call", name, digest, encode(result), True)
        self.executed += 1
        return result

    def classify_reply(self, reply: UntrustedText) -> ReplyClassification:
        return self._run("classify_reply", reply.boundary, lambda: self._inner.classify_reply(reply),
                         asdict, lambda d: ReplyClassification(**d))

    def extract_call_fields(self, transcript: UntrustedText, wanted: Sequence[str]) -> CallExtraction:
        return self._run(
            "extract_call_fields", [transcript.boundary, list(wanted)],
            lambda: self._inner.extract_call_fields(transcript, wanted),
            lambda r: {k: asdict(v) for k, v in r.fields.items()},
            lambda d: CallExtraction({k: ExtractedField(**v) for k, v in d.items()}))

    def draft_email(self, request: DraftRequest) -> DraftProposal:
        digest = [request.subject_template, request.body_template, dict(request.context),
                  {k: asdict(v) for k, v in request.evidence.items.items()},
                  list(request.feedback)]
        return self._run("draft_email", digest, lambda: self._inner.draft_email(request),
                         lambda r: {"line": r.personalised_line, "claims": [asdict(c) for c in r.claims]},
                         lambda d: DraftProposal(d["line"], tuple(
                             Claim(c["text"], tuple(c["evidence"])) for c in d["claims"])))

    def verify_claims(self, draft: Draft, evidence: Evidence) -> Sequence[ClaimVerdict]:
        digest = [draft.subject, draft.body, [asdict(c) for c in draft.claims],
                  {k: asdict(v) for k, v in evidence.items.items()}]
        return self._run("verify_claims", digest, lambda: self._inner.verify_claims(draft, evidence),
                         lambda r: [asdict(v) for v in r], lambda d: [ClaimVerdict(**v) for v in d])

    def draft_lesson(self, evidence: UntrustedText) -> LessonDraft:
        return self._run("draft_lesson", evidence.boundary, lambda: self._inner.draft_lesson(evidence),
                         lambda r: {"text": r.text, "tags": list(r.tags)},
                         lambda d: LessonDraft(d["text"], tuple(d["tags"])))


def jsonable(value: Any) -> Any:
    return json.loads(json.dumps(value, default=str))
