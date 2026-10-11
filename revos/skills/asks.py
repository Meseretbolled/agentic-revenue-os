"""How many things an email asks of its reader. An email gets exactly one question or call to action, and
never the same one twice: two asks pull the reader two ways and a repeated question reads as a glitch."""

import re

# A call to action that is not phrased as a question. Kept to the wording our templates and drafts use.
CTA = re.compile(
    r"\b(let me know|happy to (?:share|walk|send|chat|talk|show)|reply (?:to|with) (?:me|this)|"
    r"book (?:a|some)|schedule (?:a|some)|grab (?:a|some)|hop on|jump on a|set up a (?:call|time)|"
    r"a few times|times that work|send (?:me|over)|would you (?:like|be open)|can we|shall we|"
    r"let's (?:talk|chat|connect))\b", re.IGNORECASE)
_SENTENCE = re.compile(r"(?<=[.!?:])\s+|\n+")


def _key(sentence: str) -> str:
    return re.sub(r"\W+", " ", sentence.casefold()).strip()


def asks_in(body: str, *, footer: tuple[str, ...] = ()) -> list[str]:
    """The sentences that ask something of the reader (a question or a call to action), footer excluded."""
    text = body
    for line in footer:
        if line:
            text = text.replace(line, " ")
    found = []
    for sentence in _SENTENCE.split(text):
        sentence = sentence.strip()
        if sentence and (sentence.endswith("?") or CTA.search(sentence)):
            found.append(sentence)
    return found


def ask_problems(body: str, *, footer: tuple[str, ...] = ()) -> list[tuple[str, str]]:
    """(code, message) for each way the email's asks are wrong: not exactly one, or one repeated."""
    asks = asks_in(body, footer=footer)
    problems: list[tuple[str, str]] = []
    keys = [_key(a) for a in asks]
    if len(asks) != 1:
        shown = "; ".join(f'"{a[:70]}"' for a in asks) or "none"
        problems.append(("ask_count", "the email must contain exactly one question or call to action, it has "
                         f"{len(asks)} ({shown}). The email's closing line already makes the one ask: "
                         "write statements only, with no question mark and no request"))
    if len(set(keys)) < len(keys):
        problems.append(("repeated_question", "the same question or request appears more than once"))
    return problems
