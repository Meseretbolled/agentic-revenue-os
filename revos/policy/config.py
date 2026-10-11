"""Policy configuration as plain data.

A segment pack supplies `levels`, `max_discount_pct`, the caps, `send_window` and
`veto_window_seconds` through PolicyConfig.from_pack(pack); the engine itself never learns where
the numbers came from.
"""

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from revos.domain.enums import AutonomyLevel

if TYPE_CHECKING:
    from revos.segments.loader import Pack

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
    "lesson_write": L.AUTO,
    "book_meeting": L.AUTO,
    "handoff": L.AUTO,
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
    plus_bases: frozenset[str] = frozenset()  # inboxes whose name+tag@ variants are also allowed
    send_window_bypass: bool = False          # SEND_WINDOW_MODE=demo_bypass; only ever true in demo mode

    @classmethod
    def from_pack(
        cls,
        pack: "Pack",
        *,
        demo_mode: bool = True,
        allowed_recipients: frozenset[str] = frozenset(),
        plus_bases: frozenset[str] = frozenset(),
        send_window_bypass: bool = False,
    ) -> "PolicyConfig":
        """The pack's autonomy levels, caps, send window and discount limit become the policy."""
        p = pack.policy
        return cls(
            levels=dict(p.autonomy_levels),
            max_discount_pct=p.max_discount_pct,
            daily_cap=p.daily_cap,
            domain_cap=p.domain_cap,
            veto_window_seconds=p.veto_window_seconds,
            send_window=SendWindow(p.send_window.start_hour, p.send_window.end_hour,
                                   tuple(p.send_window.days)),
            default_timezone=p.default_timezone,
            demo_mode=demo_mode,
            allowed_recipients=allowed_recipients,
            plus_bases=plus_bases,
            send_window_bypass=send_window_bypass and demo_mode,
        )

    @classmethod
    def from_env(cls) -> "PolicyConfig":
        addresses = {os.environ.get("PROSPECT_TEST_EMAIL", ""), os.environ.get("GMAIL_ADDRESS", "")}
        return cls(
            demo_mode=os.environ.get("REVOS_ENV", "demo") == "demo",
            allowed_recipients=frozenset(a.strip().lower() for a in addresses if a.strip()),
            plus_bases=frozenset({os.environ.get("PROSPECT_TEST_EMAIL", "").strip().lower()} - {""}),
            send_window_bypass=send_window_bypassed(),
            veto_window_seconds=int(os.environ.get("VETO_WINDOW_SECONDS", "60")),
        )


class ConfigError(ValueError):
    """The configuration is unsafe or unreadable: the app refuses to start."""


BYPASS_BANNER = "SEND WINDOW BYPASSED: demo only"


def send_window_bypassed(env: Mapping[str, str] | None = None) -> bool:
    """SEND_WINDOW_MODE=enforce (default) keeps the pack's send window. demo_bypass skips only that
    one check, and only when REVOS_ENV is explicitly `demo`; anywhere else it is a startup error."""
    env = os.environ if env is None else env
    mode = env.get("SEND_WINDOW_MODE", "").strip().lower() or "enforce"
    if mode == "enforce":
        return False
    if mode != "demo_bypass":
        raise ConfigError(f"SEND_WINDOW_MODE must be 'enforce' or 'demo_bypass', got {mode!r}")
    if env.get("REVOS_ENV", "").strip().lower() != "demo":
        raise ConfigError("SEND_WINDOW_MODE=demo_bypass is only allowed when REVOS_ENV=demo; "
                          "refusing to start")
    return True
