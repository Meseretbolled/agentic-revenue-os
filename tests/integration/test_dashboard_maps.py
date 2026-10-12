# ruff: noqa: F811
"""The live maps: the system map and the data map are drawn by our own script from read-only JSON, and every
number on them is read from the database, the code's registries or the packs."""

import re
from pathlib import Path

from fastapi.testclient import TestClient

from revos.agents.registry import AGENTS
from revos.dashboard import maps_data
from revos.policy.audit import write_audit
from tests.integration.test_dashboard import (  # noqa: F401  (fixtures)
    TUNNEL,
    app,
    cfg,
    client,
    deal_id,
    demo,
    signed,
)

STATIC = Path("revos/dashboard/static")
MAP_PAGES = ["/dashboard/architecture", "/dashboard/data-model", "/dashboard/walkthrough"]
MAP_APIS = ["/dashboard/api/architecture", "/dashboard/api/data-model", "/dashboard/api/walkthrough"]


def test_the_map_pages_render_and_load_only_our_own_script(signed, demo):
    scripts = {"/dashboard/architecture": "maps.js", "/dashboard/data-model": "maps.js",
               "/dashboard/walkthrough": "walkthrough.js"}
    for path, script in scripts.items():
        html = signed.get(path).text
        assert f'<script src="/dashboard/static/{script}"></script>' in html
        assert not re.search(r"<script(?![^>]*\bsrc=)", html)          # no inline script
    nav = signed.get("/dashboard/").text
    assert "System map" in nav and "Data map" in nav and "Deal walkthrough" in nav


def test_the_maps_need_a_signed_in_session(client):
    for path in MAP_PAGES:
        assert client.get(path).status_code == 303
    for path in MAP_APIS:
        assert client.get(path).status_code == 401


def test_the_maps_are_refused_through_the_public_tunnel(app):
    tunnel = TestClient(app, base_url=TUNNEL, follow_redirects=False)
    for path in [*MAP_PAGES, *MAP_APIS, "/dashboard/static/maps.js", "/dashboard/static/walkthrough.js"]:
        assert tunnel.get(path).status_code == 404


def test_the_system_map_numbers_come_from_the_database_and_the_code(signed, demo, conn):
    model = signed.get("/dashboard/api/architecture").json()
    assert [a["name"] for a in model["agents"]] == list(AGENTS)               # all eleven, from the registry
    assert {a["kind"] for a in model["agents"]} == {"deterministic", "judgement"}
    assert len(model["runtime"]["schedules"]) == 6
    assert {s["event"] for s in model["runtime"]["schedules"]} >= {"email.poll", "calls.poll"}
    assert [s["stage"] for s in model["stages"]] == maps_data.MAIN_PATH and len(model["stages"]) == 12
    stored = dict(conn.execute("SELECT stage, count(*) FROM deals GROUP BY stage").fetchall())
    assert {s["stage"]: s["n"] for s in model["stages"]} == {k: stored.get(k, 0) for k in maps_data.MAIN_PATH}
    assert model["runtime"]["jobs"] == dict(conn.execute("SELECT status, count(*) FROM jobs GROUP BY status").fetchall())
    assert model["policy"]["autonomy_mode"] == "autonomous" and model["policy"]["kill_switch"] is False
    assert {i["service"] for i in model["integrations"]} >= {"HubSpot", "Gmail", "Slack", "Fireflies"}


def test_the_system_map_shows_the_entry_criteria_from_the_packs(signed, demo):
    stages = {s["stage"]: s["needs"] for s in signed.get("/dashboard/api/architecture").json()["stages"]}
    assert stages["scored"][0]["fields"] == ["tier", "total_score", "score_reason"]      # the same in every pack
    assert stages["new"] == []
    assert len(stages["enriched"]) == 2                                                  # this one differs per pack


def test_the_data_map_lists_every_table_with_its_real_row_count_and_keys(signed, demo, conn):
    model = signed.get("/dashboard/api/data-model").json()
    tables = {t["name"]: t for t in model["tables"]}
    real = {r[0] for r in conn.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")}
    assert set(tables) == real
    assert tables["deals"]["rows"] == conn.execute("SELECT count(*) FROM deals").fetchone()[0] > 0
    assert tables["deals"]["pk"] == ["id"]
    assert len(model["links"]) == conn.execute("SELECT count(*) FROM pg_constraint WHERE contype = 'f'").fetchone()[0]
    assert {"from": "contacts", "cols": ["company_id"], "to": "companies"} in model["links"]
    assert "audit_log_no_change" in tables["audit_log"]["triggers"]                      # the append-only guard
    assert all(t["area"] in model["areas"] for t in tables.values())


def test_a_new_table_would_still_appear_under_other():
    assert maps_data._area("a_table_nobody_grouped") == "Other"
    assert maps_data._area("audit_log") == "Audit"


def test_the_map_code_only_reads():
    code = Path("revos/dashboard/maps_data.py").read_text().upper()
    assert not re.search(r"\b(INSERT|UPDATE|DELETE|TRUNCATE|DROP|ALTER)\b", code.split('"""', 2)[2])


def test_the_map_script_loads_nothing_from_outside_and_never_writes_html():
    for name in ("maps.js", "walkthrough.js"):
        js = (STATIC / name).read_text()
        urls = re.findall(r"https?://[^\s'\"]+", js)
        assert urls == ["http://www.w3.org/2000/svg"]                                    # the SVG namespace only
        assert "innerHTML" not in js and "eval(" not in js and "document.write" not in js


def test_the_walkthrough_has_sixteen_steps_that_only_light_real_boxes_and_arrows(signed, demo):
    model = signed.get("/dashboard/api/walkthrough").json()
    nodes, edges = {n["id"] for n in model["nodes"]}, {e["id"] for e in model["edges"]}
    assert len(model["steps"]) == 16 and len(model["stages"]) == 12
    for step in model["steps"]:
        assert set(step["nodes"]) <= nodes and set(step["edges"]) <= edges and step["stage"] in model["stages"]
    assert all(e["from"] in nodes and e["to"] in nodes for e in model["edges"])
    agents = next(n for n in model["nodes"] if n["id"] == "agents")
    assert agents["sub"] == "11: 5 rules, 6 model-judged"                         # counted from the registry


def test_what_is_recorded_under_a_step_comes_from_that_deals_own_audit_log(signed, demo, conn):
    deal = deal_id(demo, "Northwind Optical")
    write_audit(conn, "orchestrator", "deal.stage_changed", deal, {"from": "enriched", "to": "scored"})
    model = signed.get(f"/dashboard/api/walkthrough?deal={deal}").json()
    assert model["deal"]["id"] == deal
    scoring = next(s for s in model["steps"] if s["title"] == "Scoring ranks the lead")
    assert [(r["event"], r["note"]) for r in scoring["recorded"]] == [("deal.stage_changed", "enriched -> scored")]
    other = next(s for s in model["steps"] if s["title"] == "It goes out for signature (test mode)")
    assert other["recorded"] == []                                               # no matching row: nothing is filled in


def test_an_unknown_deal_falls_back_to_the_first_listed_one(signed, demo):
    model = signed.get("/dashboard/api/walkthrough?deal=not-a-deal").json()
    assert model["deal"]["id"] == model["deals"][0]["id"]


def test_the_login_page_no_longer_explains_where_the_token_lives(client):
    html = client.get("/dashboard/login").text
    assert "DASHBOARD_TOKEN" not in html and "Dashboard token" in html
