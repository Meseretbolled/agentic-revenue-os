"""HubSpot behind the CRM port.

Every record we create carries revos_demo=true and revos_key (our idempotency key). Before any
create we SEARCH by the natural key (email, domain, revos_deal_id), so a retry after a crash finds
the record instead of making a second one. As an ActionProvider it plugs into the outbox: find()
is the check-before-retry lookup, perform() does the write."""

import hashlib
from collections.abc import Mapping
from typing import Any

from revos.adapters.hubspot.client import HubSpotClient, HubSpotError, HubSpotUnavailable
from revos.adapters.hubspot.schema import CALL_FIELDS
from revos.adapters.hubspot.stages import DEFAULT_STAGE_MAP, PIPELINE, hubspot_stage
from revos.domain.errors import PermanentError
from revos.domain.stages import DealStage
from revos.ports.crm import Record

KINDS = ("contacts", "companies", "deals")
# HubSpot-defined association type ids for a note on a record (UNVERIFIED against a live portal
# until `make live-hubspot` has been run).
NOTE_ASSOCIATION = {"contacts": 202, "companies": 190, "deals": 214}
NOTE_TOKEN_PREFIX = "revoskey"
READ_PROPERTIES = ["revos_key", "revos_demo", "revos_deal_id", "revos_stage", "email", "domain",
                   "name", "dealname", "dealstage"]


def note_token(key: str) -> str:
    """An alphanumeric token written into every note so the note can be found by its key."""
    return NOTE_TOKEN_PREFIX + hashlib.sha256(key.encode()).hexdigest()[:20]


def build_deal_properties(deal: Mapping[str, Any], fields: Mapping[str, Any],
                          lesson_ids: list[int]) -> dict[str, Any]:
    """Our deal onto HubSpot properties: score, tier, reason, stage, call fields with their quotes,
    and the lessons cited."""
    props: dict[str, Any] = {"revos_stage": deal["stage"]}
    for ours, theirs in (("total_score", "revos_score"), ("tier", "revos_tier"),
                         ("score_reason", "revos_score_reason")):
        if deal.get(ours) is not None:
            props[theirs] = deal[ours]
    for name in CALL_FIELDS:
        if fields.get(name):
            props[f"revos_call_{name}"] = fields[name]
        if fields.get(f"quote:{name}"):
            props[f"revos_call_{name}_quote"] = fields[f"quote:{name}"]
    if lesson_ids:
        props["revos_lesson_ids"] = ",".join(f"L{i}" for i in lesson_ids)
    return props


class HubSpotCRM:
    def __init__(self, client: HubSpotClient, *,
                 stage_map: Mapping[DealStage, str] = DEFAULT_STAGE_MAP) -> None:
        self._c = client
        self._stages = stage_map

    # --- low-level -------------------------------------------------------------------------------
    def _find(self, kind: str, prop: str, value: str) -> dict[str, Any] | None:
        filters = [{"propertyName": prop, "operator": "EQ", "value": value}]
        return next(iter(self._c.search_all(kind, filters, READ_PROPERTIES, page_size=10)), None)

    def _create(self, kind: str, props: dict[str, Any], what: str) -> str:
        return str(self._c.request("POST", f"/crm/v3/objects/{kind}", json={"properties": props},
                                   what=what, retry_ambiguous=False)["id"])

    def _find_after_doubt(self, kind: str, natural: tuple[str, str], key: str) -> dict[str, Any] | None:
        """HubSpot's search lags a little behind writes, so look a few times before concluding the
        record does not exist."""
        for wait in (0.0, 0.5, 1.5):
            if wait:
                self._c._sleep(wait)
            found = self._find(kind, *natural) or self._find(kind, "revos_key", key)
            if found:
                return found
        return None

    def _update(self, kind: str, record_id: str, props: dict[str, Any], what: str) -> None:
        self._c.request("PATCH", f"/crm/v3/objects/{kind}/{record_id}", json={"properties": props},
                        what=what)

    def _upsert(self, kind: str, natural: tuple[str, str], props: dict[str, Any],
                key: str, on_create: Mapping[str, Any] | None = None) -> tuple[str, bool]:
        """Search by the natural key; update if found, create if not. A create that loses a race
        (409) falls back to the update path."""
        found = self._find(kind, *natural)
        if found:
            self._update(kind, found["id"], {**props, "revos_demo": "true"}, f"update {kind}")
            return str(found["id"]), False
        body = {**(on_create or {}), **props, natural[0]: natural[1], "revos_demo": "true",
                "revos_key": key}
        for _ in range(3):
            try:
                return self._create(kind, body, f"create {kind}"), True
            except HubSpotError as exc:              # 409: someone (or an earlier try) made it
                if exc.status != 409:
                    raise
                again = self._find_after_doubt(kind, natural, key)
                if again is None:
                    raise
                self._update(kind, again["id"], props, f"update {kind}")
                return str(again["id"]), False
            except HubSpotUnavailable:               # the answer was lost: did the create happen?
                again = self._find_after_doubt(kind, natural, key)
                if again is not None:
                    return str(again["id"]), False
        raise HubSpotUnavailable(f"create {kind}", "gave up after three tries")

    def associate(self, from_kind: str, from_id: str, to_kind: str, to_id: str) -> None:
        self._c.request("PUT", f"/crm/v4/objects/{from_kind}/{from_id}/associations/default/"
                               f"{to_kind}/{to_id}", what=f"associate {from_kind} with {to_kind}")

    # --- typed operations ---------------------------------------------------------------------------
    def upsert_contact(self, email: str, props: Mapping[str, Any], key: str) -> tuple[str, bool]:
        return self._upsert("contacts", ("email", email.strip().lower()), dict(props), key)

    def upsert_company(self, domain: str, props: Mapping[str, Any], key: str) -> tuple[str, bool]:
        return self._upsert("companies", ("domain", domain.strip().lower()), dict(props), key)

    def upsert_deal(self, deal_id: str, props: Mapping[str, Any], key: str, *,
                    stage: str | DealStage | None = None, contact_id: str | None = None,
                    company_id: str | None = None) -> tuple[str, bool]:
        body = dict(props)
        if stage is not None:
            body["dealstage"] = hubspot_stage(stage, self._stages)
            body["pipeline"] = PIPELINE
            body["revos_stage"] = DealStage(stage).value          # the exact stage, kept as well
        body["revos_deal_id"] = deal_id
        record_id, created = self._upsert("deals", ("revos_deal_id", deal_id), body, key,
                                          on_create={"dealname": f"Revos deal {deal_id[:8]}"})
        for kind, other in (("contacts", contact_id), ("companies", company_id)):
            if other:
                self.associate("deals", record_id, kind, other)       # a PUT: safe to repeat
        return record_id, created

    def add_note_to(self, kind: str, record_id: str, text: str, key: str) -> str:
        """A note on a record. Looked up by its key token first, so a retry adds no second note."""
        existing = self._find_note(key)
        if existing:
            return existing
        body = {"properties": {"hs_note_body": f"{text}\n\n[{note_token(key)}] revosdemo",
                               "hs_timestamp": _now_ms()},
                "associations": [{"to": {"id": record_id}, "types": [{
                    "associationCategory": "HUBSPOT_DEFINED",
                    "associationTypeId": NOTE_ASSOCIATION[kind]}]}]}
        for _ in range(3):
            try:
                return str(self._c.request("POST", "/crm/v3/objects/notes", json=body,
                                           what="create note", retry_ambiguous=False)["id"])
            except HubSpotUnavailable:
                for wait in (0.0, 0.5, 1.5):         # search lags writes: look before trying again
                    if wait:
                        self._c._sleep(wait)
                    existing = self._find_note(key)
                    if existing:
                        return existing
        raise HubSpotUnavailable("create note", "gave up after three tries")

    def _find_note(self, key: str) -> str | None:
        filters = [{"propertyName": "hs_note_body", "operator": "CONTAINS_TOKEN",
                    "value": note_token(key)}]
        hit = next(iter(self._c.search_all("notes", filters, ["hs_note_body"], page_size=10)), None)
        return str(hit["id"]) if hit else None

    def deal_id_for(self, revos_deal_id: str) -> str | None:
        found = self._find("deals", "revos_deal_id", revos_deal_id)
        return str(found["id"]) if found else None

    # --- the CRM port ---------------------------------------------------------------------------------
    def upsert(self, kind: str, key: str, properties: Record) -> str:
        if kind not in KINDS:
            raise PermanentError(f"HubSpot cannot upsert {kind!r}")
        props = dict(properties)
        if kind == "contacts":
            return self.upsert_contact(_need(props, "email"), props, key)[0]
        if kind == "companies":
            return self.upsert_company(_need(props, "domain"), props, key)[0]
        return self.upsert_deal(_need(props, "revos_deal_id"), props, key)[0]

    def add_note(self, key: str, record_id: str, text: str) -> str:
        return self.add_note_to("deals", record_id, text, key)

    def get_record(self, kind: str, record_id: str) -> Record | None:
        try:
            got = self._c.request("GET", f"/crm/v3/objects/{kind}/{record_id}",
                                  params={"properties": ",".join(READ_PROPERTIES)}, what=f"get {kind}")
        except HubSpotError as exc:
            if exc.status == 404:
                return None
            raise
        return {"id": got["id"], **got.get("properties", {})}

    def list_records(self, kind: str) -> list[Record]:
        return [{"id": r["id"], **r.get("properties", {})}
                for r in self._c.list_all(kind, READ_PROPERTIES)]

    def find_by_key(self, key: str) -> str | None:
        for kind in KINDS:
            hit = self._find(kind, "revos_key", key)
            if hit:
                return str(hit["id"])
        return self._find_note(key)

    # --- ActionProvider: what the outbox calls -------------------------------------------------------
    def find(self, idempotency_key: str) -> str | None:
        return self.find_by_key(idempotency_key)

    def perform(self, action: Mapping[str, Any]) -> str:
        """crm_update writes a contact, company or deal; crm_note adds a note to a deal.

        Identity comes from our database (the contact row's email, the deal id), never from the
        payload."""
        payload, key = action["payload"], action["idempotency_key"]
        if action["type"] == "crm_note":
            hubspot_id = self.deal_id_for(str(payload["record_id"]))
            if hubspot_id is None:
                raise PermanentError("the deal is not in HubSpot yet, so a note cannot be added")
            return self.add_note_to("deals", hubspot_id, str(payload["text"]), key)
        if action["type"] != "crm_update":
            raise PermanentError(f"HubSpot cannot perform {action['type']!r}")
        kind, props = payload["kind"], dict(payload.get("properties", {}))
        if kind == "contacts":
            return self.upsert_contact(_need({"email": action.get("to_email")}, "email"), props, key)[0]
        if kind == "companies":
            return self.upsert_company(_need(payload, "domain"), props, key)[0]
        if kind == "deals":
            email, domain = action.get("to_email"), payload.get("company_domain")
            contact = self._find("contacts", "email", email.lower()) if email else None
            company = self._find("companies", "domain", domain.lower()) if domain else None
            return self.upsert_deal(str(action["deal_id"]), props, key, stage=payload.get("stage"),
                                    contact_id=contact["id"] if contact else None,
                                    company_id=company["id"] if company else None)[0]
        raise PermanentError(f"HubSpot cannot write {kind!r}")


def _need(source: Mapping[str, Any], name: str) -> str:
    value = source.get(name)
    if not value:
        raise PermanentError(f"HubSpot needs {name} to find or create this record")
    return str(value)


def _now_ms() -> str:
    import time
    return str(int(time.time() * 1000))
