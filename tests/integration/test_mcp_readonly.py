"""No tool can send, sign, approve or change a stage. Checked from four sides."""

import ast
import dataclasses
import re
from pathlib import Path

import psycopg
import pytest

from revos.mcp.agents import AGENT_SCOPES, REGISTRY
from revos.mcp.registry import ToolContext
from tests.mcp_helpers import call, is_blocked, make_ctx, make_deal

MCP = Path(__file__).resolve().parents[2] / "revos" / "mcp"
ALLOWED_REVOS_IMPORTS = ("revos.domain", "revos.mcp", "revos.ports.research", "revos.segments")
WRITE_SQL = re.compile(r"\b(INSERT|UPDATE|DELETE|TRUNCATE|DROP|ALTER|CREATE|GRANT)\b", re.IGNORECASE)
READ_VERBS = re.compile(r"^(get|list|pick|search|fetch)")
WRITE_WORDS = ("send", "sign", "approve", "transition", "write", "update", "create", "delete",
               "propose", "post", "set_", "execute", "stage", "perform")


def mcp_sources():
    for path in sorted(MCP.glob("*.py")):
        yield path.name, ast.parse(path.read_text())


def test_mcp_modules_import_nothing_that_can_act() -> None:
    offenders = []
    for name, tree in mcp_sources():
        for node in ast.walk(tree):
            modules = ([node.module] if isinstance(node, ast.ImportFrom) and node.module else
                       [a.name for a in node.names] if isinstance(node, ast.Import) else [])
            for module in modules:
                if module.startswith("revos") and not module.startswith(ALLOWED_REVOS_IMPORTS):
                    offenders.append(f"{name}: {module}")
    assert not offenders, offenders        # no outbox, gate, policy, orchestrator, mailer, e-sign


def test_mcp_modules_contain_no_write_sql() -> None:
    hits = [f"{name}: {node.value[:60]!r}" for name, tree in mcp_sources() for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
            and WRITE_SQL.search(node.value)]
    assert not hits, hits


def test_every_tool_is_a_read_by_name() -> None:
    for key, spec in REGISTRY.items():
        assert READ_VERBS.match(spec.name), key
        assert not any(w in spec.name.lower() for w in WRITE_WORDS), key
    assert {s.server for s in REGISTRY.values()} == {"crm", "lessons", "casestudies", "research"}


def test_a_tool_context_holds_no_way_to_send_or_write() -> None:
    names = {f.name for f in dataclasses.fields(ToolContext)}
    assert names == {"conn", "packs", "research", "scope_provider", "on_blocked", "on_quarantine"}


def test_the_connection_tools_get_cannot_write(ro_conn) -> None:
    for sql in ("INSERT INTO system_settings (key, value) VALUES ('x', '1')",
                "UPDATE deals SET stage = 'handoff'", "DELETE FROM actions"):
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            ro_conn.execute(sql)


def snapshot(conn) -> dict[str, str]:
    tables = [r[0] for r in conn.execute(
        "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY 1")]
    return {t: conn.execute(f"SELECT md5(coalesce(string_agg(x::text, '|' ORDER BY x::text), '')) "
                            f"FROM {t} x").fetchone()[0] for t in tables}


def test_running_every_tool_changes_nothing_in_the_database(conn, ro_conn, pack) -> None:
    deal = make_deal(conn, "brightside-optical.example", "sam@brightside-optical.example",
                     fields='{"budget": "fifty thousand"}')
    lesson = conn.execute("INSERT INTO lessons (text, evidence) VALUES ('t', 'q') RETURNING id"
                          ).fetchone()[0]
    ctx, _ = make_ctx(ro_conn, pack, deal["deal"])
    args = {"deal_id": str(deal["deal"]), "contact_id": str(deal["contact"]),
            "company_id": str(deal["company"]), "lesson_id": lesson, "query": "brightside",
            "case_study_id": "pearle_vision", "url": "https://brightside-optical.example/", "tags": []}
    before = snapshot(conn)
    ran = 0
    for key, spec in REGISTRY.items():
        agent = next(a for a, keys in AGENT_SCOPES.items() if key in keys)
        result = call(ctx, agent, key, **{k: v for k, v in args.items() if k in spec.properties})
        assert not is_blocked(result), (key, result)
        ran += 1
    assert ran == len(REGISTRY) == 10
    assert snapshot(conn) == before


def test_no_tool_exists_for_sending_signing_or_moving_a_deal(world=None) -> None:
    forbidden = {"send_email", "send_io", "sign", "transition_deal", "approve", "post"}
    assert not forbidden & {spec.name for spec in REGISTRY.values()}
    assert not forbidden & {k.split(".")[1] for k in REGISTRY}
