"""Rule-based field extraction from page text, driven by the pack's extractors."""

import re
from dataclasses import dataclass

from revos.segments.schema import Extractor

_SENTENCE = re.compile(r"(?<=[.!?])\s+")


@dataclass(frozen=True)
class Extracted:
    field: str
    value: int | str | bool
    quote: str            # the sentence the value was found in, verbatim


def _sentences(text: str) -> list[str]:
    sentences = []
    for line in text.splitlines():
        sentences += [s.strip() for s in _SENTENCE.split(" ".join(line.split())) if s.strip()]
    return sentences


def _one(extractor: Extractor, sentence: str) -> int | str | bool | None:
    if extractor.kind == "number":
        match = re.search(extractor.pattern or "", sentence, re.IGNORECASE)
        return int(match.group(1).replace(",", "")) if match else None
    if extractor.kind == "keyword":
        low = sentence.casefold()
        for value, words in (extractor.map or {}).items():
            if any(w.casefold() in low for w in words):
                return value
        return None
    low = sentence.casefold()
    return True if any(w.casefold() in low for w in extractor.any or []) else None


def extract_fields(text: str, extractors: list[Extractor]) -> list[Extracted]:
    """The first sentence that yields a value wins for each field."""
    found: list[Extracted] = []
    sentences = _sentences(text)
    for extractor in extractors:
        for sentence in sentences:
            value = _one(extractor, sentence)
            if value is not None:
                found.append(Extracted(extractor.field, value, sentence))
                break
    return found
