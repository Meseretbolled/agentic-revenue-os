"""Who may receive mail in demo mode.

An address passes only if it is listed exactly, or it is the owner's prospect inbox with a plus
tag: the domain is the same as the base address, and the local part is exactly the base local part,
optionally followed by `+tag`. Nothing else passes: `name2@`, `name.x+tag@` (Gmail ignores dots,
we do not), `name+x@other.com` and anything with a second `@` are all refused."""

import re
from collections.abc import Iterable

_TAG = re.compile(r"[a-z0-9][a-z0-9._-]{0,39}")


def _split(address: str) -> tuple[str, str] | None:
    parts = address.strip().lower().split("@")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        return None
    return parts[0], parts[1]


def base_of(address: str) -> str:
    """name+tag@gmail.com -> name@gmail.com (an address without a tag is returned unchanged)."""
    split = _split(address)
    return f"{split[0].split('+')[0]}@{split[1]}" if split else ""


def is_plus_variant(address: str, base: str) -> bool:
    """True for the base address itself and for base-local+tag@base-domain."""
    got, want = _split(address), _split(base)
    if got is None or want is None or got[1] != want[1]:
        return False
    want_local = want[0].split("+")[0]
    if got[0] == want_local:
        return True
    local, plus, tag = got[0].partition("+")
    return bool(plus) and local == want_local and bool(_TAG.fullmatch(tag))


def recipient_allowed(address: str, exact: Iterable[str], plus_bases: Iterable[str] = ()) -> bool:
    address = address.strip().lower()
    return address in set(exact) or any(is_plus_variant(address, b) for b in plus_bases if b)
