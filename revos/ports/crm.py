from typing import Any, Protocol

Record = dict[str, Any]


class CRM(Protocol):
    """The external CRM (HubSpot). Postgres stays the source of truth; this mirrors it."""

    def upsert(self, kind: str, key: str, properties: Record) -> str:
        """Create or update a record. The same key always yields the same record id."""
        ...

    def add_note(self, key: str, record_id: str, text: str) -> str: ...

    def get_record(self, kind: str, record_id: str) -> Record | None: ...

    def list_records(self, kind: str) -> list[Record]: ...

    def find_by_key(self, key: str) -> str | None:
        """Record or note id already created for this key, if any."""
        ...
