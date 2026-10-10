from collections.abc import Iterable
from dataclasses import dataclass, field


@dataclass(frozen=True)
class EvidenceItem:
    id: str
    text: str                    # the quote or approved fact itself
    source_url: str | None = None
    stale: bool = False          # too old to be called recent


@dataclass(frozen=True)
class Evidence:
    items: dict[str, EvidenceItem] = field(default_factory=dict)

    @classmethod
    def of(cls, items: Iterable[EvidenceItem]) -> "Evidence":
        return cls({i.id: i for i in items})


@dataclass(frozen=True)
class Claim:
    """A statement about the prospect (or a proof point) and the evidence it rests on."""

    text: str
    evidence: tuple[str, ...]


@dataclass(frozen=True)
class Draft:
    subject: str
    body: str
    claims: tuple[Claim, ...]


@dataclass(frozen=True)
class Issue:
    code: str
    message: str                 # written so it can be fed back to the drafter
    text: str | None = None

    @property
    def infrastructure(self) -> bool:
        """The verifier was unavailable: regenerating the draft cannot help."""
        return self.code.startswith("verifier_") and self.code != "verifier_rejected"
