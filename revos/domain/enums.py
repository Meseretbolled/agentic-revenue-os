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
