"""The new text of an email reply, without the history it quotes.

A reply carries our own earlier message underneath ("On Sun ... wrote:" and lines starting with
">"). Classifying or reading intent from that history would classify our own words, so only the
text above it counts."""

import re

_ATTRIBUTION = re.compile(r"\n[ \t]*(?:On\s.{0,300}?wrote:|-{2,}\s*Original Message\s*-{2,}|"
                          r"Le\s.{0,300}?a écrit\s*:|Am\s.{0,300}?schrieb\b.{0,100}?:)", re.S | re.I)


def new_text(body: str) -> str:
    text = "\n" + body.replace("\r\n", "\n")
    text = _ATTRIBUTION.split(text, maxsplit=1)[0]
    kept = [line for line in text.split("\n") if not line.lstrip().startswith(">")]
    return "\n".join(kept).strip()


def quote_around(text: str, needle: str, limit: int = 200) -> str:
    """The sentence of `text` that contains `needle` (case-insensitive), trimmed. Empty if none."""
    for sentence in re.split(r"(?<=[.!?])\s+|\n+", text):
        if needle.casefold() in sentence.casefold():
            sentence = " ".join(sentence.split())
            return sentence if len(sentence) <= limit else sentence[:limit - 1] + "…"
    return ""
