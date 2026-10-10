"""No agent has a tool or a code path that sends, signs or changes a stage."""

import ast
from pathlib import Path

AGENTS = Path(__file__).resolve().parents[2] / "revos" / "agents"
PROCEDURES = ["enrichment", "scoring", "outreach", "reply", "meeting", "call_intel",
              "qualification", "contract_io", "tracking", "knowledge"]
# What an agent procedure may import from this repository.
ALLOWED = ("revos.agents.common", "revos.agents.types", "revos.agents.outreach", "revos.domain",
           "revos.skills", "revos.ports.llm", "revos.segments.schema")     # schema = plain config data
FORBIDDEN_NAMES = {"conn", "execute", "transition_deal", "propose_action", "send_with_gate",
                   "execute_action", "run_approved_action", "write_audit", "record_event"}


def tree(name: str) -> ast.AST:
    return ast.parse((AGENTS / f"{name}.py").read_text())


def test_agent_procedures_import_only_pure_helpers() -> None:
    offenders = []
    for name in PROCEDURES:
        for node in ast.walk(tree(name)):
            modules = ([node.module] if isinstance(node, ast.ImportFrom) and node.module else
                       [a.name for a in node.names] if isinstance(node, ast.Import) else [])
            offenders += [f"{name}: {m}" for m in modules
                          if m.startswith("revos") and not m.startswith(ALLOWED)]
    assert not offenders, offenders


def test_agent_procedures_never_name_anything_that_acts() -> None:
    hits = []
    for name in PROCEDURES:
        for node in ast.walk(tree(name)):
            ident = node.id if isinstance(node, ast.Name) else node.attr if isinstance(
                node, ast.Attribute) else None
            if ident in FORBIDDEN_NAMES:
                hits.append(f"{name}:{node.lineno} {ident}")
    assert not hits, hits


def test_the_agent_environment_holds_no_way_to_act() -> None:
    import dataclasses

    from revos.agents.types import AgentEnv
    names = {f.name for f in dataclasses.fields(AgentEnv)}
    assert not names & {"conn", "mailer", "crm", "esign", "provider", "outbox", "store"}
    assert names == {"agent", "step", "runner", "deal_id", "contact_id", "company_id", "pack", "tools",
                     "clock", "identity", "input", "untrusted", "slots", "judge", "verifier"}


def test_only_the_orchestrator_applies_proposals_and_moves_stages() -> None:
    root = AGENTS.parent
    movers, appliers = set(), set()
    for path in sorted(root.rglob("*.py")):
        text = path.read_text()
        rel = path.relative_to(root).as_posix()
        if "transition_deal(" in text and not rel.startswith(("orchestrator/",)):
            movers.add(rel)
        if "propose_action(" in text and rel not in {"policy/service.py"} \
                and not rel.startswith("orchestrator/"):
            appliers.add(rel)
    assert not movers and not appliers, (movers, appliers)


def test_a_proposal_is_plain_data() -> None:
    import dataclasses

    from revos.agents.types import Proposal
    assert dataclasses.is_dataclass(Proposal)
    assert [f.name for f in dataclasses.fields(Proposal)] == [
        "action_type", "target", "payload", "evidence_ids", "reason"]
