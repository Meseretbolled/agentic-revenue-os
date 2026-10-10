from collections.abc import Mapping
from typing import Any

from revos.adapters.mock.fixtures import load
from revos.ports.crm import Record


class MockCRM:
    """In-memory HubSpot stand-in. Upserts are idempotent by key; every call is recorded."""

    def __init__(self, seed: bool = True) -> None:
        self._records: dict[str, dict[str, Record]] = {}
        self._keys: dict[str, str] = {}
        self.calls: list[tuple[str, str]] = []
        if seed:
            for item in load("crm_seed")["records"]:
                self.upsert(item["kind"], item["key"], item["properties"])
            self.calls.clear()

    def upsert(self, kind: str, key: str, properties: Record) -> str:
        self.calls.append(("upsert", key))
        table = self._records.setdefault(kind, {})
        record_id = self._keys.get(key)
        if record_id is None:
            record_id = f"{kind[:3]}-{len(table) + 1}"
            self._keys[key] = record_id
            table[record_id] = {"id": record_id}
        table[record_id].update(properties)
        return record_id

    def add_note(self, key: str, record_id: str, text: str) -> str:
        self.calls.append(("note", key))
        if key in self._keys:
            return self._keys[key]
        note_id = f"note-{len(self._keys) + 1}"
        self._keys[key] = note_id
        self._records.setdefault("notes", {})[note_id] = {"id": note_id, "record": record_id,
                                                           "text": text}
        return note_id

    def get_record(self, kind: str, record_id: str) -> Record | None:
        record = self._records.get(kind, {}).get(record_id)
        return dict(record) if record else None

    def list_records(self, kind: str) -> list[Record]:
        return [dict(r) for r in self._records.get(kind, {}).values()]

    def find_by_key(self, key: str) -> str | None:
        return self._keys.get(key)

    # --- ActionProvider ---
    def find(self, idempotency_key: str) -> str | None:
        return self.find_by_key(idempotency_key)

    def perform(self, action: Mapping[str, Any]) -> str:
        payload = action["payload"]
        if action["type"] == "crm_update":
            natural = {"deals": action.get("deal_id") and f"deal:{action['deal_id']}",
                       "contacts": action.get("to_email") and f"contact:{action['to_email']}",
                       "companies": payload.get("domain") and f"company:{payload['domain']}"
                       }.get(payload["kind"])
            props = {**payload["properties"], **({"stage": payload["stage"]} if payload.get("stage") else {})}
            return self.upsert(payload["kind"], natural or action["idempotency_key"], props)
        if action["type"] == "crm_note":
            return self.add_note(action["idempotency_key"], payload["record_id"], payload["text"])
        raise ValueError(f"CRM cannot perform {action['type']!r}")
