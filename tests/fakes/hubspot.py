"""An in-memory HubSpot for tests: just enough of the CRM v3 API, behind httpx.MockTransport.

It checks the Authorization header, so a test can prove the token is sent as a Bearer token."""

import json
import re
from collections import deque
from typing import Any

import httpx

TOKEN = "test-token-not-a-real-secret"


class FakeHubSpot:
    def __init__(self, token: str = TOKEN) -> None:
        self.token = token
        self.objects: dict[str, dict[str, dict[str, Any]]] = {k: {} for k in
                                                              ("contacts", "companies", "deals", "notes", "emails")}
        self.properties: dict[str, dict[str, dict[str, Any]]] = {k: {} for k in
                                                                 ("contacts", "companies", "deals")}
        self.groups: dict[str, set[str]] = {k: set() for k in ("contacts", "companies", "deals")}
        self.associations: list[tuple[str, str, str, str]] = []
        self.association_types: list[tuple[str, str, int, str]] = []
        self.pipeline_stages = ["appointmentscheduled", "qualifiedtobuy", "presentationscheduled",
                                "decisionmakerboughtin", "contractsent", "closedwon", "closedlost"]
        self.calls: list[tuple[str, str]] = []
        self.headers_seen: list[dict[str, str]] = []
        self.fail_next: deque[int] = deque()           # statuses to answer before behaving
        self.fail_create: deque[int] = deque()         # statuses for the next creates: nothing saved
        self.retry_after: str | None = None
        self.after_create: Any = None                  # hook(kind) called after a create succeeds
        self.page_size = 100
        self.owner_list: list[dict[str, Any]] = []          # GET /crm/v3/owners
        self.owners_forbidden = False                       # the service key lacks crm.objects.owners.read
        self._next = 1000

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def client(self) -> httpx.Client:
        return httpx.Client(transport=self.transport())

    def new_id(self) -> str:
        self._next += 1
        return str(self._next)

    # --- request handling --------------------------------------------------------------------------
    def handle(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        self.calls.append((method, path))
        self.headers_seen.append(dict(request.headers))
        if request.headers.get("authorization") != f"Bearer {self.token}":
            return httpx.Response(401, json={"status": "error", "category": "INVALID_AUTHENTICATION"})
        if self.fail_next:
            status = self.fail_next.popleft()
            headers = {"Retry-After": self.retry_after} if self.retry_after else {}
            return httpx.Response(status, json={"status": "error"}, headers=headers)
        body = json.loads(request.content) if request.content else {}
        return self.route(method, path, dict(request.url.params), body)

    def route(self, method: str, path: str, params: dict[str, str], body: dict[str, Any]) -> httpx.Response:
        if path == "/crm/v3/owners":
            if self.owners_forbidden:
                return httpx.Response(403, json={"status": "error", "category": "MISSING_SCOPES",
                                                 "message": "This app hasn't been granted all required scopes"})
            return httpx.Response(200, json={"results": self.owner_list})
        if m := re.fullmatch(r"/crm/v3/objects/(\w+)/search", path):
            return self.search(m[1], body)
        if m := re.fullmatch(r"/crm/v3/objects/(\w+)", path):
            kind = m[1]
            return self.create(kind, body) if method == "POST" else self.list(kind, params)
        if m := re.fullmatch(r"/crm/v3/objects/(\w+)/(\w+)", path):
            kind, record_id = m[1], m[2]
            record = self.objects[kind].get(record_id)
            if record is None:
                return httpx.Response(404, json={"status": "error"})
            if method == "PATCH":
                record["properties"].update(body["properties"])
            elif method == "DELETE":
                del self.objects[kind][record_id]
                return httpx.Response(204)
            return httpx.Response(200, json=record)
        if m := re.fullmatch(r"/crm/v4/objects/(\w+)/(\w+)/associations/default/(\w+)/(\w+)", path):
            self.associations.append((m[1], m[2], m[3], m[4]))
            return httpx.Response(200, json={})
        if m := re.fullmatch(r"/crm/v3/properties/(\w+)/groups", path):
            if method == "POST":
                if body["name"] in self.groups[m[1]]:
                    return httpx.Response(409, json={})
                self.groups[m[1]].add(body["name"])
                return httpx.Response(201, json=body)
            return httpx.Response(200, json={"results": [{"name": g} for g in self.groups[m[1]]]})
        if m := re.fullmatch(r"/crm/v3/properties/(\w+)", path):
            kind = m[1]
            if method == "POST":
                if body["name"] in self.properties[kind]:
                    return httpx.Response(409, json={})
                self.properties[kind][body["name"]] = body
                return httpx.Response(201, json=body)
            return httpx.Response(200, json={"results": list(self.properties[kind].values())})
        if path == "/crm/v3/pipelines/deals":
            return httpx.Response(200, json={"results": [{"id": "default", "stages": [
                {"id": s} for s in self.pipeline_stages]}]})
        return httpx.Response(404, json={"status": "error", "message": f"unhandled {method} {path}"})

    def create(self, kind: str, body: dict[str, Any]) -> httpx.Response:
        if self.fail_create:
            return httpx.Response(self.fail_create.popleft(), json={"status": "error"})
        props = body["properties"]
        if kind == "contacts" and any(r["properties"].get("email") == props.get("email")
                                      for r in self.objects[kind].values()):
            return httpx.Response(409, json={"message": "Contact already exists"})
        if props.get("revos_key") and any(r["properties"].get("revos_key") == props["revos_key"]
                                          for r in self.objects[kind].values()):
            return httpx.Response(409, json={"message": "unique value already exists"})
        record_id = self.new_id()
        self.objects[kind][record_id] = {"id": record_id, "properties": dict(props)}
        for assoc in body.get("associations", []):
            self.associations.append((kind, record_id, "?", assoc["to"]["id"]))
            self.association_types.append((kind, record_id, assoc["types"][0]["associationTypeId"],
                                           assoc["to"]["id"]))
        if self.after_create:
            override = self.after_create(kind)           # may raise, or return a different answer
            if override is not None:
                return override
        return httpx.Response(201, json=self.objects[kind][record_id])

    def matches(self, record: dict[str, Any], flt: dict[str, str]) -> bool:
        have = record["properties"].get(flt["propertyName"])
        if flt["operator"] == "EQ":
            return str(have).lower() == str(flt["value"]).lower() if have is not None else False
        if flt["operator"] == "CONTAINS_TOKEN":
            return flt["value"] in re.findall(r"[A-Za-z0-9]+", str(have or ""))
        return False

    def search(self, kind: str, body: dict[str, Any]) -> httpx.Response:
        filters = body["filterGroups"][0]["filters"]
        hits = [r for r in self.objects[kind].values() if all(self.matches(r, f) for f in filters)]
        start = int(body.get("after", 0))
        size = min(body.get("limit", 100), self.page_size)
        page = hits[start:start + size]
        out: dict[str, Any] = {"total": len(hits), "results": page}
        if start + size < len(hits):
            out["paging"] = {"next": {"after": str(start + size)}}
        return httpx.Response(200, json=out)

    def list(self, kind: str, params: dict[str, str]) -> httpx.Response:
        records = list(self.objects[kind].values())
        start = int(params.get("after", 0))
        size = min(int(params.get("limit", 100)), self.page_size)
        out: dict[str, Any] = {"results": records[start:start + size]}
        if start + size < len(records):
            out["paging"] = {"next": {"after": str(start + size)}}
        return httpx.Response(200, json=out)
