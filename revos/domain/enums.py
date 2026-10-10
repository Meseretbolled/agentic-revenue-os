from enum import StrEnum


class Tier(StrEnum):
    A = "A"
    B = "B"
    C = "C"
    DISQUALIFIED = "disqualified"


class AutonomyLevel(StrEnum):
    AUTO = "auto"
    AUTO_WITH_VETO = "auto_with_veto"
    NEEDS_APPROVAL = "needs_approval"


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ActionStatus(StrEnum):
    PENDING = "pending"
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    CANCELLED = "cancelled"
    SENT = "sent"
    FAILED = "failed"


class Decision(StrEnum):
    APPROVED = "approved"
    VETOED = "vetoed"
    REJECTED = "rejected"


class AutonomyMode(StrEnum):
    AUTONOMOUS = "autonomous"
    APPROVAL = "approval"       # brief-compliant: every auto_with_veto becomes needs_approval


class Outcome(StrEnum):
    AUTO = "auto"
    AUTO_WITH_VETO = "auto_with_veto"
    NEEDS_APPROVAL = "needs_approval"
    DRAFT_ONLY = "draft_only"   # never sent by the system (social messages)
    BLOCKED = "blocked"         # hard block: goes to a person, never sends
    DENIED = "denied"           # not on the allowlist
