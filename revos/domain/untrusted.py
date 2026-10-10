"""UntrustedText: the one way inbound text (replies, web pages, transcripts) enters the system.

A model can only be given such text through for_model(), which refuses flagged content. The block
it produces is fenced by a boundary id derived from the content itself, so the text cannot close
its own block and start "real" instructions.
"""

import hashlib
import re
from dataclasses import dataclass
from typing import Literal

from revos.domain.errors import QuarantinedContent
from revos.domain.injection import Finding, extract_html, normalise, scan

Kind = Literal["reply", "web_page", "transcript"]
_LOOKALIKE = re.compile(r"<<\s*/?\s*untrusted", re.IGNORECASE)


@dataclass(frozen=True)
class UntrustedText:
    kind: Kind
    origin: str                      # url, message id or transcript id
    _raw: str
    _visible: str
    hidden_text: tuple[str, ...]
    findings: tuple[Finding, ...]
    boundary: str

    @classmethod
    def wrap(cls, kind: Kind, origin: str, content: str, *, is_html: bool = False,
             max_chars: int | None = None) -> "UntrustedText":
        if is_html:
            visible, hidden = extract_html(content)
        else:
            visible, hidden = content.strip(), ()
        if max_chars is not None:
            visible = visible[:max_chars]       # cut before hashing, so the fence stays intact
        findings = scan(visible)
        for piece in hidden:
            findings += scan(piece, hidden=True)
        digest = hashlib.sha256(f"{kind}\n{origin}\n{visible}".encode()).hexdigest()[:16]
        return cls(kind, origin, content, visible, hidden, tuple(findings), digest)

    @property
    def flagged(self) -> bool:
        return bool(self.findings)

    @property
    def quarantined(self) -> bool:
        return self.flagged

    def for_model(self) -> str:
        """The only route to a prompt. Raises QuarantinedContent for flagged text."""
        if self.flagged:
            raise QuarantinedContent(self.findings)
        origin = re.sub(r'[\s"<>]+', "_", self.origin)[:200]
        body = _LOOKALIKE.sub("<< UNTRUSTED-LOOKALIKE", self._visible)
        return (f'<<UNTRUSTED kind="{self.kind}" origin="{origin}" boundary="{self.boundary}">>\n'
                f"{body}\n"
                f'<</UNTRUSTED boundary="{self.boundary}">>')

    def for_human(self) -> dict[str, object]:
        """What a reviewer sees: the text as received, what matched, and any hidden text."""
        return {"kind": self.kind, "origin": self.origin, "text": self._raw,
                "hidden_text": list(self.hidden_text), "boundary": self.boundary,
                "findings": [{"rule": f.rule_id, "snippet": f.snippet, "hidden": f.hidden}
                             for f in self.findings]}

    def snippets(self, pattern: str) -> list[str]:
        """Code-extracted matches of a regex from the visible text (for example a date). Only the
        matched snippets leave; the text itself does not."""
        return [m.group(0) for m in re.finditer(pattern, self._visible, re.IGNORECASE)]

    def contains_quote(self, quote: str) -> bool:
        """True if `quote` appears in the visible text (whitespace and case ignored)."""
        needle = normalise(quote)
        return bool(needle) and needle in normalise(self._visible)
