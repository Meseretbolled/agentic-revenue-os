import json

import pytest

from revos.domain.errors import ExitCriteriaNotMet
from revos.domain.stages import DealStage
from revos.orchestrator.transitions import transition_deal


def deal_at(conn, stage_path, fields=None):
    deal = conn.execute("INSERT INTO deals (segment_id, fields) VALUES ('test_seg', %s) RETURNING id",
                        (json.dumps(fields or {}),)).fetchone()[0]
    for stage in stage_path:
        transition_deal(conn, deal, stage, actor="setup", reason="setup")
    return deal


def test_pack_exit_criteria_gate_the_qualified_stage(conn, pack) -> None:
    deal = deal_at(conn, [DealStage.ENRICHED, DealStage.SCORED, DealStage.OUTREACH,
                          DealStage.REPLIED, DealStage.MEETING])
    criteria = pack.stages.exit_criteria
    with pytest.raises(ExitCriteriaNotMet) as err:
        transition_deal(conn, deal, DealStage.QUALIFIED, actor="q", reason="r",
                        exit_criteria=criteria)
    assert err.value.missing == ["budget", "timeline", "decision_maker", "needs"]
    conn.execute("UPDATE deals SET fields = %s WHERE id = %s", (json.dumps(
        {"budget": "x", "timeline": "y", "decision_maker": "z", "needs": "n"}), deal))
    transition_deal(conn, deal, DealStage.QUALIFIED, actor="q", reason="r", exit_criteria=criteria)


def test_a_false_flag_does_not_satisfy_a_criterion(conn, pack) -> None:
    path = [DealStage.ENRICHED, DealStage.SCORED, DealStage.OUTREACH, DealStage.REPLIED,
            DealStage.MEETING, DealStage.QUALIFIED, DealStage.PROPOSAL]
    deal = deal_at(conn, path, {"io_validated": False})
    with pytest.raises(ExitCriteriaNotMet, match="io_validated"):
        transition_deal(conn, deal, DealStage.IO_APPROVAL, actor="q", reason="r",
                        exit_criteria=pack.stages.exit_criteria)
