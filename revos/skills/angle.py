"""Lessons shape the ANGLE of a draft: which approved message to lead with. They never put their
own words, or anyone else's facts, into an email."""

import re
from collections.abc import Mapping, Sequence
from typing import Any

from revos.segments.schema import Pillar


def _words(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]{4,}", text.casefold()))


def choose_pillar(pillars: Sequence[Pillar], lessons: Sequence[Mapping[str, Any]]
                  ) -> tuple[Pillar, list[int]]:
    """The pillar the retrieved lessons point at, and the ids of the lessons that pointed at it.

    No lesson overlaps any pillar: the first pillar, and no lesson is cited as having shaped it."""
    best, best_score, best_ids = pillars[0], 0.0, []
    for pillar in pillars:
        vocabulary = _words(f"{pillar.id} {pillar.headline} {pillar.body}")
        score, ids = 0.0, []
        for lesson in lessons:
            overlap = vocabulary & _words(" ".join(lesson.get("tags", [])) + " " + lesson["text"])
            if overlap:
                score += len(overlap) * float(lesson.get("confidence", 0.5))
                ids.append(int(lesson["id"]))
        if score > best_score:
            best, best_score, best_ids = pillar, score, ids
    return best, best_ids
