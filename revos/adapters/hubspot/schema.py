"""The custom properties revos needs in HubSpot, and the idempotent setup that creates them."""

from dataclasses import dataclass, field
from typing import Any

from revos.adapters.hubspot.client import HubSpotClient, HubSpotError
from revos.adapters.hubspot.stages import DEFAULT_STAGE_MAP, PIPELINE

GROUP = "revos"
CALL_FIELDS = ("budget", "timeline", "decision_maker", "signer", "needs", "objections", "competitors",
               "next_steps")


@dataclass(frozen=True)
class PropertySpec:
    name: str
    label: str
    kind: str                       # text | textarea | number | bool
    unique: bool = False

    def body(self) -> dict[str, Any]:
        base: dict[str, Any] = {"name": self.name, "label": self.label, "groupName": GROUP}
        if self.kind == "number":
            return {**base, "type": "number", "fieldType": "number"}
        if self.kind == "bool":
            return {**base, "type": "enumeration", "fieldType": "booleancheckbox",
                    "options": [{"label": "Yes", "value": "true", "displayOrder": 0},
                                {"label": "No", "value": "false", "displayOrder": 1}]}
        body = {**base, "type": "string", "fieldType": "textarea" if self.kind == "textarea" else "text"}
        return {**body, "hasUniqueValue": True} if self.unique else body


_COMMON = [PropertySpec("revos_demo", "Revos demo record", "bool"),
           PropertySpec("revos_key", "Revos idempotency key", "text", unique=True)]

PROPERTIES: dict[str, list[PropertySpec]] = {
    "contacts": list(_COMMON),
    "companies": list(_COMMON),
    "deals": [
        *_COMMON,
        PropertySpec("revos_deal_id", "Revos deal id", "text", unique=True),
        PropertySpec("revos_stage", "Revos stage", "text"),
        PropertySpec("revos_score", "Revos score", "number"),
        PropertySpec("revos_tier", "Revos tier", "text"),
        PropertySpec("revos_score_reason", "Revos score reason", "textarea"),
        PropertySpec("revos_outreach_status", "Revos outreach status", "text"),
        PropertySpec("revos_sequence_day", "Revos sequence day", "number"),
        PropertySpec("revos_lesson_ids", "Revos lessons cited", "text"),
        *[PropertySpec(f"revos_call_{name}", f"Revos call: {name.replace('_', ' ')}", "textarea")
          for name in CALL_FIELDS],
        *[PropertySpec(f"revos_call_{name}_quote", f"Revos call quote: {name.replace('_', ' ')}", "textarea")
          for name in CALL_FIELDS],
    ],
}


@dataclass
class SetupReport:
    created: list[str] = field(default_factory=list)       # "deals.revos_score"
    existing: list[str] = field(default_factory=list)
    groups_created: list[str] = field(default_factory=list)
    stages_missing: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.created or self.groups_created)


def _ensure_group(client: HubSpotClient, object_type: str, report: SetupReport) -> None:
    groups = client.request("GET", f"/crm/v3/properties/{object_type}/groups",
                            what=f"list property groups for {object_type}")
    if any(g["name"] == GROUP for g in groups.get("results", [])):
        return
    try:
        client.request("POST", f"/crm/v3/properties/{object_type}/groups",
                       json={"name": GROUP, "label": "Revenue OS", "displayOrder": -1},
                       what=f"create property group for {object_type}")
        report.groups_created.append(object_type)
    except HubSpotError as exc:
        if exc.status != 409:                              # 409: another run made it first
            raise


def ensure_properties(client: HubSpotClient) -> SetupReport:
    """Create what is missing and nothing else, so running it twice changes nothing."""
    report = SetupReport()
    for object_type, specs in PROPERTIES.items():
        _ensure_group(client, object_type, report)
        have = {p["name"] for p in client.request(
            "GET", f"/crm/v3/properties/{object_type}", what=f"list properties for {object_type}"
        ).get("results", [])}
        for spec in specs:
            label = f"{object_type}.{spec.name}"
            if spec.name in have:
                report.existing.append(label)
                continue
            try:
                client.request("POST", f"/crm/v3/properties/{object_type}", json=spec.body(),
                               what=f"create property {label}")
                report.created.append(label)
            except HubSpotError as exc:
                if exc.status != 409:
                    raise
                report.existing.append(label)
    report.stages_missing = missing_stages(client)
    return report


def missing_stages(client: HubSpotClient, pipeline: str = PIPELINE) -> list[str]:
    """Stage ids our map points at that the account's deal pipeline does not have."""
    try:
        pipelines = client.request("GET", "/crm/v3/pipelines/deals", what="list deal pipelines")
    except HubSpotError:
        return []
    found = {s["id"] for p in pipelines.get("results", []) if p.get("id") == pipeline
             for s in p.get("stages", [])}
    return sorted(set(DEFAULT_STAGE_MAP.values()) - found) if found else []
