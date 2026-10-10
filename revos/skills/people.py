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
