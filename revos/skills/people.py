"""Reading a person's name and role out of a sentence like 'Dana Park, our CMO, is the decision maker.'"""

import re
from dataclasses import dataclass

_NAME = r"(?P<name>[A-Z][\w'’-]+(?:\s+[A-Z][\w'’-]+)+)"
_ROLE = r"(?P<role>[A-Za-z][\w &/-]*?)"
_PATTERNS = (
    re.compile(rf"{_NAME},?\s+(?:our|the|their|my)\s+{_ROLE}(?=,|\s+is\b|\s+will\b|\s+and\b|\.|$)"),
    re.compile(rf"{_NAME}\s+is\s+(?:our|the|their|my)\s+{_ROLE}(?=,|\s+and\b|\.|$)"),
)


@dataclass(frozen=True)
class Person:
    name: str
    role: str


def parse_person_role(quote: str) -> Person | None:
    """The first name and role the sentence states, or None. Both come from the quote itself."""
    for pattern in _PATTERNS:
        if match := pattern.search(quote):
            role = match.group("role").strip()
            if role and role.lower() not in {"decision maker", "decision-maker"}:
                return Person(match.group("name").strip(), role)
    return None


_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")


@dataclass(frozen=True)
class Signer:
    name: str
    role: str
    source: str            # "call" (the quote states the name) or "lead_record" (the call gave none)


def resolve_signer(call_quote: str | None, record_name: str | None,
                   record_role: str | None) -> Signer | None:
    """Who signs. The call's own words win when its quote names a person and a role; otherwise the lead
    record's signer. A name or role that contains an email address is never accepted: an address is not a
    name, and the signer's address comes from the lead record alone. None when neither has a name."""
    person = parse_person_role(call_quote) if call_quote else None
    if person and not _EMAIL.search(f"{person.name} {person.role}"):
        return Signer(person.name, person.role, "call")
    name = (record_name or "").strip()
    if name and not _EMAIL.search(name):
        return Signer(name, (record_role or "").strip(), "lead_record")
    return None
