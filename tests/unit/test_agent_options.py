import asyncio
import json

import pytest

from revos.adapters.mock import MockResearch
from revos.mcp import agents
from revos.mcp.agents import AGENT_SCOPES, agent_options, build_servers, mcp_tool_names
from revos.mcp.registry import ToolContext


class Capture:
    """Stands in for create_sdk_mcp_server so the tools it would serve can be inspected."""

    def __init__(self) -> None:
        self.servers: dict[str, list] = {}

    def __call__(self, name, version="1.0.0", tools=None):
        self.servers[name] = list(tools or [])
        return {"type": "sdk", "name": name, "tools": tools}


@pytest.fixture
def ctx():
    return ToolContext(conn=None, packs={}, research=MockResearch(), scope_provider=lambda: None)


def test_options_give_no_builtin_tools_no_host_settings_and_only_the_agents_tools(ctx) -> None:
    options = agent_options("outreach", ctx, system_prompt="p", model="m", max_turns=4)
    assert options.tools == [] and options.setting_sources == []
    assert options.max_turns == 4 and options.model == "m"
    assert sorted(options.allowed_tools) == sorted(mcp_tool_names("outreach"))
    assert "mcp__research__fetch_site" not in options.allowed_tools
    assert set(options.mcp_servers) == {"crm", "lessons", "casestudies"}


@pytest.mark.parametrize("agent", sorted(AGENT_SCOPES))
def test_each_agent_gets_exactly_its_declared_tools(agent, ctx, monkeypatch) -> None:
    capture = Capture()
    monkeypatch.setattr(agents, "create_sdk_mcp_server", capture)
    build_servers(agent, ctx)
    served = {f"{server}.{t.name}" for server, tools in capture.servers.items() for t in tools}
    assert served == set(AGENT_SCOPES[agent])


def test_a_served_tool_runs_through_the_guard(ctx, monkeypatch) -> None:
    capture = Capture()
    monkeypatch.setattr(agents, "create_sdk_mcp_server", capture)
    build_servers("scoring", ctx)
    handler = next(t for t in capture.servers["crm"] if t.name == "get_deal").handler
    result = asyncio.run(handler({"deal_id": "00000000-0000-0000-0000-000000000000"}))
    assert result["is_error"] and "scope could not be resolved" in result["content"][0]["text"]
    assert json.dumps(result)


def test_an_unknown_agent_cannot_get_options(ctx) -> None:
    with pytest.raises(KeyError):
        agent_options("freelancer", ctx, system_prompt="p", model="m")
