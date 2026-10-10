import asyncio
import json
from dataclasses import dataclass, field

from revos.adapters.mock import MockResearch
from revos.mcp.agents import build_servers  # noqa: F401  (re-exported for tests)
from revos.mcp.registry import ToolContext, call_tool
from revos.mcp.scope import fixed_scope
from tests.conftest import PACK_ID


@dataclass
class Seen:
    blocks: list = field(default_factory=list)
    quarantines: list = field(default_factory=list)


def make_deal(conn, domain: str, email: str, *, segment: str = PACK_ID, fields: str = "{}"):
    conn.execute("INSERT INTO segments (id, pack_version) VALUES (%s, '1') ON CONFLICT DO NOTHING",
                 (segment,))
    company = conn.execute(
        "INSERT INTO companies (segment_id, name, domain) VALUES (%s, %s, %s) RETURNING id",
        (segment, domain.split(".")[0].title(), domain)).fetchone()[0]
    contact = conn.execute(
        "INSERT INTO contacts (company_id, email, full_name, timezone) VALUES (%s, %s, 'Sam Lee', "
        "'America/Chicago') RETURNING id", (company, email)).fetchone()[0]
    deal = conn.execute(
        "INSERT INTO deals (segment_id, company_id, contact_id, fields) VALUES (%s, %s, %s, %s) "
        "RETURNING id", (segment, company, contact, fields)).fetchone()[0]
    return {"deal": deal, "company": company, "contact": contact}


def make_ctx(ro_conn, pack, deal_id, *, research=None, provider=None) -> tuple[ToolContext, Seen]:
    seen = Seen()
    ctx = ToolContext(
        conn=ro_conn, packs={pack.id: pack}, research=research or MockResearch(),
        scope_provider=provider or fixed_scope(ro_conn, deal_id),
        on_blocked=seen.blocks.append,
        on_quarantine=lambda content, deal: seen.quarantines.append((content, deal)),
    )
    return ctx, seen


def call(ctx, agent, key, **args):
    return call_tool(ctx, agent, key, args)


def is_blocked(result) -> bool:
    return bool(result.get("is_error")) and result["content"][0]["text"].startswith("BLOCKED")


def data(result) -> dict:
    assert not result.get("is_error"), result
    return json.loads(result["content"][0]["text"])


def run_async(coro):
    return asyncio.run(coro)
