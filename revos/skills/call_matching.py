"""Which deal a finished call belongs to. Evidence only, never a guess.

Three kinds of evidence, in this order of preference:
  1. a participant's email is the contact's email,
  2. the title contains the deal's short id or the company's name,
  3. the call started within a window of the booked meeting.

Each rule narrows the candidates only if at least one deal survives it. When exactly one deal is left
the call is matched; more than one (or none) is reported as ambiguous (or none) and the caller holds
the call for a person."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from revos.policy.recipients import base_of
from revos.ports.transcripts import Transcript

MEETING_WINDOW = timedelta(hours=3)


@dataclass(frozen=True)
class DealFacts:
    deal_id: UUID
    company: str
    contact_email: str
    meeting_start: datetime | None


@dataclass(frozen=True)
class CallMatch:
    kind: str                       # matched | ambiguous | none
    deal_id: UUID | None = None
    candidates: tuple[UUID, ...] = ()
    by: tuple[str, ...] = ()        # the rules that narrowed it


def short_id(deal_id: UUID) -> str:
    return str(deal_id)[:8]


def match_call(transcript: Transcript, deals: list[DealFacts]) -> CallMatch:
    if not deals:
        return CallMatch("none")
    emails = {p.strip().lower() for p in transcript.participants if "@" in p}
    exact = [d for d in deals if d.contact_email.lower() in emails]
    # a plus-addressed lead (name+acme@) is the same inbox as its base address
    shared = [d for d in deals if base_of(d.contact_email) in {base_of(e) for e in emails}]
    title = transcript.title.lower()
    by_title = [d for d in deals if short_id(d.deal_id) in title
                or (d.company and d.company.lower() in title)]
    by_time = [d for d in deals if d.meeting_start is not None
               and abs(transcript.date - d.meeting_start) <= MEETING_WINDOW]

    pool, used = list(deals), []
    rules = (("participant_email", exact or shared), ("title", by_title), ("meeting_time", by_time))
    for name, hits in rules:
        narrowed = [d for d in pool if d in hits]
        if narrowed:
            pool = narrowed
            used.append(name)
        if len(pool) == 1 and used:
            break
    if not used:
        return CallMatch("none")
    ids = tuple(d.deal_id for d in pool)
    if len(pool) == 1:
        return CallMatch("matched", pool[0].deal_id, ids, tuple(used))
    return CallMatch("ambiguous", None, ids, tuple(used))
