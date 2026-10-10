class RevosError(Exception):
    """Base class for domain errors."""


class DealNotFound(RevosError):
    pass


class IllegalTransition(RevosError):
    pass


class ExitCriteriaNotMet(RevosError):
    def __init__(self, stage: str, missing: list[str]) -> None:
        self.stage = stage
        self.missing = missing
        super().__init__(f"cannot enter {stage}: missing {', '.join(missing)}")


class StaleDeal(RevosError):
    """The deal changed since the caller read it (optimistic lock)."""


class PermanentError(RevosError):
    """A job failure that retrying cannot fix: skip retries, go to the dead-letter table."""


class ActionNotApproved(RevosError):
    """The database holds no approving decision for this action, so it must not run."""


class LLMError(RevosError):
    """The model call failed or returned something unusable. Callers fail closed."""


class SpendCapExceeded(LLMError):
    """The LLM spend cap would be exceeded, so the call was not made."""


class QuarantinedContent(RevosError):
    """Flagged inbound text was about to be given to a model. It goes to a person instead."""

    def __init__(self, findings: tuple) -> None:
        self.findings = findings
        super().__init__("inbound content is quarantined: "
                         + ", ".join(sorted({f.rule_id for f in findings})))
