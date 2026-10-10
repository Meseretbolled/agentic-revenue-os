"""Delete the demo records we created in HubSpot, and only those.

A record is deleted only if the search returned it AND its revos_demo property reads 'true' when
we look again. Anything else is left alone, whatever it is."""

from collections.abc import Iterable

from revos.adapters.hubspot.client import HubSpotClient, HubSpotError

ORDER = ("deals", "contacts", "companies")      # children before parents
FLAG = [{"propertyName": "revos_demo", "operator": "EQ", "value": "true"}]


def find_demo_records(client: HubSpotClient) -> dict[str, list[str]]:
    """Ids of everything flagged revos_demo=true (and our notes), without deleting anything."""
    found: dict[str, list[str]] = {}
    for kind in ORDER:
        found[kind] = [str(r["id"]) for r in client.search_all(kind, FLAG, ["revos_demo"])
                       if (r.get("properties") or {}).get("revos_demo") == "true"]
    marker = [{"propertyName": "hs_note_body", "operator": "CONTAINS_TOKEN", "value": "revosdemo"}]
    found["notes"] = [str(r["id"]) for r in client.search_all("notes", marker, ["hs_note_body"])
                      if "revosdemo" in ((r.get("properties") or {}).get("hs_note_body") or "")]
    return found


def reset_demo(client: HubSpotClient, *, extra_note_ids: Iterable[str] = (),
               dry_run: bool = False) -> dict[str, int]:
    """Delete the flagged records. Returns how many of each kind were (or would be) deleted."""
    found = find_demo_records(client)
    found["notes"] = sorted(set(found["notes"]) | {str(i) for i in extra_note_ids})
    counts: dict[str, int] = {}
    for kind in (*ORDER, "notes"):
        deleted = 0
        for record_id in found.get(kind, []):
            if not dry_run:
                try:
                    client.request("DELETE", f"/crm/v3/objects/{kind}/{record_id}", what=f"delete {kind}")
                except HubSpotError as exc:
                    if exc.status != 404:          # already gone is fine
                        raise
            deleted += 1
        counts[kind] = deleted
    return counts


