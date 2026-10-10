from revos.adapters.fake_llm import FakeLLM
from revos.domain.untrusted import UntrustedText
from revos.runtime.quarantine import record_quarantine
from revos.skills.inbound import triage_reply
from tests.unit.test_inbound import DEMO_REPLY, DEMO_REPLY_HTML


def make_deal(conn):
    return conn.execute("INSERT INTO deals (segment_id) VALUES ('test_seg') RETURNING id").fetchone()[0]


def test_demo_reply_is_logged_with_what_matched_and_nothing_happens_to_the_deal(conn) -> None:
    deal = make_deal(conn)
    inbound = UntrustedText.wrap("reply", "msg-7", DEMO_REPLY)
    triage = triage_reply(FakeLLM(), inbound)
    assert triage.route == "quarantine"
    record_quarantine(conn, inbound, deal)

    (actor, event, detail), = conn.execute(
        "SELECT actor, event, detail FROM audit_log WHERE deal_id = %s", (deal,)).fetchall()
    assert (actor, event) == ("quarantine", "inbound.quarantined")
    assert detail["origin"] == "msg-7" and detail["boundary"] == inbound.boundary
    assert detail["findings"][0]["rule"] == "override.en"
    assert "ignore previous instructions" in detail["findings"][0]["snippet"]
    assert "held for a person" in detail["action"]
    # not obeyed: no action, no stage change, no discount anywhere
    assert conn.execute("SELECT count(*) FROM actions").fetchone()[0] == 0
    assert conn.execute("SELECT stage FROM deals WHERE id = %s", (deal,)).fetchone()[0] == "new"
    assert "90" not in str(detail["findings"][0].get("rule"))


def test_hidden_html_instruction_is_logged_as_hidden(conn) -> None:
    inbound = UntrustedText.wrap("reply", "msg-8", DEMO_REPLY_HTML, is_html=True)
    record_quarantine(conn, inbound)
    detail = conn.execute("SELECT detail FROM audit_log").fetchone()[0]
    assert detail["findings"][0]["hidden"] is True and detail["hidden_text_pieces"] == 1
