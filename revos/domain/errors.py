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
