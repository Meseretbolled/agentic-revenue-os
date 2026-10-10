"""A full offline setup: real Postgres, the real pack, the mock world, the fake LLM."""

from dataclasses import dataclass

from revos.adapters.fake_llm import FakeLLM
from revos.adapters.mock import MockWorld, build_mock_world
from revos.agents.runner import RulesRunner
from revos.agents.types import Identity
from revos.orchestrator.pipeline import Orchestrator
from tests.conftest import PACK_ID

CONTACT_EMAIL = "sam.lee@brightside-optical.example"


@dataclass
class Rig:
    orch: Orchestrator
    world: MockWorld
    llm: FakeLLM
    deal_id: object
    contact_id: object


def make_lead(conn, *, domain="brightside-optical.example", company="Brightside Optical Group",
              email=CONTACT_EMAIL, name="Sam Lee", fields="{}", segment=PACK_ID):
    conn.execute("INSERT INTO segments (id, pack_version) VALUES (%s, '1') ON CONFLICT DO NOTHING",
                 (segment,))
    company_id = conn.execute("INSERT INTO companies (segment_id, name, domain) VALUES (%s, %s, %s) "
                              "RETURNING id", (segment, company, domain)).fetchone()[0]
    contact_id = conn.execute("INSERT INTO contacts (company_id, email, full_name, timezone) "
                              "VALUES (%s, %s, %s, 'UTC') RETURNING id",
                              (company_id, email, name)).fetchone()[0]
    deal_id = conn.execute("INSERT INTO deals (segment_id, company_id, contact_id, fields) "
                           "VALUES (%s, %s, %s, %s) RETURNING id",
                           (segment, company_id, contact_id, fields)).fetchone()[0]
    return deal_id, contact_id


def make_rig(conn, ro_conn, pack, clock, *, llm=None, world=None, runner=None, **lead) -> Rig:
    world = world or build_mock_world()
    llm = llm or FakeLLM()
    deal_id, contact_id = make_lead(conn, **lead)
    orch = Orchestrator(
        conn, ro_conn, clock=clock, packs={pack.id: pack}, runner=runner or RulesRunner(llm),
        research=world.research, provider=world.router, identity=Identity(),
        demo_mode=True, allowed_recipients=frozenset({CONTACT_EMAIL, "owner@example.test"}),
        calendar=world.calendar, transcripts=world.transcripts)
    return Rig(orch, world, llm, deal_id, contact_id)


def force_stage(conn, deal_id, stage) -> None:
    """Walk a deal to `stage` along the legal path (for test set-up only)."""
    from revos.domain.stages import DealStage
    from revos.orchestrator.transitions import transition_deal
    path = [DealStage.ENRICHED, DealStage.SCORED, DealStage.OUTREACH, DealStage.REPLIED,
            DealStage.MEETING, DealStage.QUALIFIED, DealStage.PROPOSAL, DealStage.IO_APPROVAL]
    current = DealStage(conn.execute("SELECT stage FROM deals WHERE id = %s", (deal_id,)).fetchone()[0])
    for nxt in path[path.index(current) + 1 if current in path else 0:]:
        if DealStage(conn.execute("SELECT stage FROM deals WHERE id = %s", (deal_id,)).fetchone()[0]) is stage:
            break
        transition_deal(conn, deal_id, nxt, actor="test", reason="set-up")


def audit(conn, event: str, deal_id=None) -> list[dict]:
    q = "SELECT detail FROM audit_log WHERE event = %s" + (" AND deal_id = %s" if deal_id else "")
    return [r[0] for r in conn.execute(q, (event, deal_id) if deal_id else (event,))]


class Crash(Exception):
    """An unexpected failure that is not one of the typed errors (a process dying, say)."""
