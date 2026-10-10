"""Policy configuration as plain data.

PACK PLUG-IN POINT (Step 4): a segment pack supplies `levels`, `max_discount_pct`, the caps,
`send_window` and `veto_window_seconds`. Build it with a PolicyConfig.from_pack(pack) there; the
engine itself never learns where the numbers came from.
"""

import os
from collections.abc import Mapping
from dataclasses import dataclass, field

from revos.domain.enums import AutonomyLevel

L = AutonomyLevel

# Action types that reach a person outside the company: gated, capped, kill-switchable.
OUTBOUND_TYPES = frozenset({"first_touch", "follow_up", "send_io"})
# Outbound types whose body is model-written text, so a claim check applies.
EMAIL_BODY_TYPES = frozenset({"first_touch", "follow_up"})
# Never sent by the system; queued as a draft for a person.
DRAFT_ONLY_TYPES = frozenset({"social_message"})

PRICE_RULE_KEY = "price_message"

# The allowlist, in the shape a pack will provide. Keys are action types; first_touch has one
# entry per tier. Anything not listed here is denied.
DEFAULT_LEVELS: Mapping[str, AutonomyLevel] = {
    "enrich": L.AUTO,
    "score": L.AUTO,
    "crm_update": L.AUTO,
    "crm_note": L.AUTO,
    "first_touch.A": L.AUTO_WITH_VETO,
    "first_touch.B": L.AUTO,
    "first_touch.C": L.AUTO,
    "follow_up": L.AUTO,
    "reminder": L.AUTO,
    "classify_reply": L.AUTO,
    "snooze_ooo": L.AUTO,
    "suppress_contact": L.AUTO,
    "cancel_scheduled_sends": L.AUTO,
    "attach_case_study": L.AUTO,
    "generate_io": L.AUTO,
    "send_io": L.AUTO,
    "social_message": L.NEEDS_APPROVAL,
    PRICE_RULE_KEY: L.AUTO_WITH_VETO,
}


@dataclass(frozen=True)
class SendWindow:
    start_hour: int = 9
    end_hour: int = 17
    days: tuple[int, ...] = (0, 1, 2, 3, 4)   # Monday=0


@dataclass(frozen=True)
class PolicyConfig:
    levels: Mapping[str, AutonomyLevel] = field(default_factory=lambda: dict(DEFAULT_LEVELS))
    max_discount_pct: float | None = 15.0     # sample value; the pack owns the real one
    daily_cap: int = 50
    domain_cap: int = 5
    veto_window_seconds: int = 60
    send_window: SendWindow = field(default_factory=SendWindow)
    default_timezone: str = "UTC"             # used when a contact has no timezone
    cap_timezone: str = "UTC"                 # where "a day" starts for the caps
    demo_mode: bool = True                    # fail closed: empty allowlist means nobody
    allowed_recipients: frozenset[str] = frozenset()

    @classmethod
    def from_env(cls) -> "PolicyConfig":
        addresses = {os.environ.get("PROSPECT_TEST_EMAIL", ""), os.environ.get("GMAIL_ADDRESS", "")}
        return cls(
            demo_mode=os.environ.get("REVOS_ENV", "demo") == "demo",
            allowed_recipients=frozenset(a.strip().lower() for a in addresses if a.strip()),
            veto_window_seconds=int(os.environ.get("VETO_WINDOW_SECONDS", "60")),
        )
