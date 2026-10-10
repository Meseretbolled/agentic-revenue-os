"""Lessons shape the ANGLE of a draft: which approved message to lead with. They never put their
own words, or anyone else's facts, into an email."""

import re
from collections.abc import Mapping, Sequence
from typing import Any

from revos.segments.schema import Pillar

TAG_WEIGHT = 5


def _words(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]{4,}", text.casefold()))


def choose_pillar(pillars: Sequence[Pillar], lessons: Sequence[Mapping[str, Any]]
                  ) -> tuple[Pillar, list[int]]:
    """The pillar the retrieved lessons point at, and the ids of the lessons that pointed at it,
    strongest first.

    No lesson overlaps any pillar: the first pillar, and no lesson is cited as having shaped it."""
    best, best_score, best_ids = pillars[0], 0.0, []
    for pillar in pillars:
        vocabulary = _words(f"{pillar.id} {pillar.headline} {pillar.body}")
        contributions: list[tuple[float, int]] = []
        for lesson in lessons:
            tag_hits = vocabulary & _words(" ".join(lesson.get("tags", [])))
            text_hits = vocabulary & _words(lesson["text"])
            if tag_hits or text_hits:
                # a curated tag is a much stronger signal than words that happen to overlap
                weight = (len(text_hits) + TAG_WEIGHT * len(tag_hits)) * float(lesson.get("confidence", 0.5))
                contributions.append((weight, int(lesson["id"])))
        score = sum(w for w, _ in contributions)
        if score > best_score:
            best, best_score = pillar, score
            best_ids = [i for _, i in sorted(contributions, key=lambda c: (-c[0], c[1]))]
    return best, best_ids
