"""Is this web result really about THIS lead's company? Pure rules, no model.

A result is accepted only if all three agree with the lead: the page is on the company's own domain, the
company's distinctive name appears in the text, and (when the lead's location is known) the location appears
too. Anything else is rejected with the reasons, so a search that returns unrelated real companies (a known
failure: a search for an invented company returned real, unrelated ones) can never put their facts on this
lead."""

import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import yaml

# words that say what kind of company it is, not which one (plain data in a YAML file, not in code)
_GENERIC = frozenset(yaml.safe_load(
    (Path(__file__).resolve().parent.parent / "fixtures" / "generic_name_words.yaml").read_text())["words"])
_WORD = re.compile(r"[a-z0-9]+")


def _words(text: str) -> list[str]:
    return _WORD.findall(text.casefold().replace("&", " and "))


def normalise_name(name: str) -> str:
    return " ".join(_words(name))


def distinctive_tokens(name: str) -> list[str]:
    """The words that identify the company. A name made only of generic words keeps all of them."""
    words = _words(name)
    keep = [w for w in words if w not in _GENERIC]
    return keep or words


def host_matches(url: str, domain: str) -> bool:
    host = (urlparse(url).hostname or "").lower().removeprefix("www.")
    wanted = domain.lower().removeprefix("www.")
    return bool(host) and bool(wanted) and (host == wanted or host.endswith("." + wanted))


def name_matches(text: str, company: str) -> bool:
    present = set(_words(text))
    tokens = distinctive_tokens(company)
    return bool(tokens) and all(t in present for t in tokens)


def location_matches(text: str, location: str) -> bool:
    low = " ".join(_words(text))
    parts = [p.strip() for p in re.split(r"[,/;]", location) if len(p.strip()) >= 3]
    return any(" ".join(_words(p)) in low for p in parts if _words(p))


@dataclass(frozen=True)
class MatchDecision:
    accepted: bool
    reasons: tuple[str, ...]


def decide(*, url: str, text: str, company: str, domain: str, location: str | None = None,
           title: str = "") -> MatchDecision:
    """Accept only when the domain, the name and (if known) the location all match the lead."""
    reasons: list[str] = []
    if not host_matches(url, domain):
        host = urlparse(url).hostname or "(no host)"
        reasons.append(f"domain: {host} is not {domain}")
    haystack = f"{title}\n{text}"
    if not name_matches(haystack, company):
        reasons.append(f"name: '{' '.join(distinctive_tokens(company))}' not found in the text")
    if location and not location_matches(haystack, location):
        reasons.append(f"location: '{location}' not found in the text")
    return MatchDecision(not reasons, tuple(reasons))
