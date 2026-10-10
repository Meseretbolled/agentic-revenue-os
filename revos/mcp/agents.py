"""Which agent may use which tool, and how an agent's SDK options are built."""

from typing import Any

from claude_agent_sdk import ClaudeAgentOptions, create_sdk_mcp_server, tool

from revos.mcp import tools_casestudies, tools_crm, tools_lessons, tools_research
from revos.mcp.registry import ToolContext, ToolSpec, call_tool

REGISTRY: dict[str, ToolSpec] = {
    spec.key: spec
    for module in (tools_crm, tools_lessons, tools_casestudies, tools_research)
    for spec in module.SPECS
}

# Declared allowlists, one per agent (PROJECT_BRIEF.md section 6.10). Tools for calendar,
# transcripts, IO and audit arrive with the steps that need them.
AGENT_SCOPES: dict[str, frozenset[str]] = {
    "enrichment": frozenset({"crm.get_deal", "crm.get_company", "research.search",
                             "research.fetch_site"}),
    "scoring": frozenset({"crm.get_deal", "crm.get_company", "crm.get_contact"}),
    "outreach": frozenset({"crm.get_deal", "crm.get_contact", "crm.get_company", "lessons.search",
                           "lessons.get", "casestudies.list", "casestudies.pick",
                           "casestudies.get"}),
    "reply": frozenset({"crm.get_deal", "crm.get_contact", "lessons.search", "casestudies.pick",
                        "casestudies.get"}),
    "meeting": frozenset({"crm.get_deal", "crm.get_contact", "lessons.search"}),
    "call_intel": frozenset({"crm.get_deal"}),
    "qualification": frozenset({"crm.get_deal"}),
    "contract_io": frozenset({"crm.get_deal", "crm.get_company", "crm.get_contact"}),
    "tracking": frozenset({"crm.get_deal"}),
    "knowledge": frozenset({"lessons.search", "lessons.get"}),
}


def tools_for(agent: str) -> list[ToolSpec]:
    """The agent's declared tools. An unknown agent has none (KeyError), never a default set."""
    return [REGISTRY[key] for key in sorted(AGENT_SCOPES[agent])]


def mcp_tool_names(agent: str) -> list[str]:
    return [f"mcp__{spec.server}__{spec.name}" for spec in tools_for(agent)]


def build_servers(agent: str, ctx: ToolContext) -> dict[str, Any]:
    """One in-process MCP server per tool group, holding only this agent's tools."""
    servers: dict[str, Any] = {}
    for server in sorted({spec.server for spec in tools_for(agent)}):
        sdk_tools = []
        for spec in (s for s in tools_for(agent) if s.server == server):
            def make(spec: ToolSpec = spec):
                @tool(spec.name, spec.description, spec.schema)
                async def handler(args: dict[str, Any]) -> dict[str, Any]:
                    return call_tool(ctx, agent, spec.key, args)
                return handler
            sdk_tools.append(make())
        servers[server] = create_sdk_mcp_server(name=server, tools=sdk_tools)
    return servers


def agent_options(agent: str, ctx: ToolContext, *, system_prompt: str, model: str,
                  max_turns: int = 8) -> ClaudeAgentOptions:
    """SDK options for one agent: no built-in tools, no host settings, only its own MCP tools."""
    return ClaudeAgentOptions(
        system_prompt=system_prompt,
        model=model,
        max_turns=max_turns,
        tools=[],                         # none of the SDK's built-in tools (Bash, Read, Write...)
        setting_sources=[],               # no host settings or CLAUDE.md
        mcp_servers=build_servers(agent, ctx),
        allowed_tools=mcp_tool_names(agent),
        permission_mode="default",
    )
