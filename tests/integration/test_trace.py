"""revos trace: one deal's timeline from a full mock slice."""

import json

import pytest
from typer.testing import CliRunner

from revos.cli.main import app
from revos.trace import find_deals, timeline
from tests.pipeline_helpers import make_rig

STAGES = ["enriched", "scored", "outreach"]


def full_slice(conn, ro_conn, pack, clock):
    from revos.ports.mail import InboundEmail
    rig = make_rig(conn, ro_conn, pack, clock, crm_sync=True)
    first = rig.orch.handle_lead_created(rig.deal_id)
    assert rig.orch.send_action(first.action_id).status == "sent"
    rig.orch.handle_reply(rig.deal_id, InboundEmail(
        "<in1@x>", "sam.lee@brightside-optical.example", "me@x", "Re: hello",
        "Your pricing is too expensive for us. Our CFO reviews every vendor in the second quarter.",
        clock.now()))
    return rig


def test_the_timeline_has_each_stage_policy_decision_outbox_action_and_email(conn, ro_conn, pack, clock):
    rig = full_slice(conn, ro_conn, pack, clock)
    entries = timeline(conn, str(rig.deal_id))
    text = "\n".join(e.line() for e in entries)

    for stage in STAGES:                                             # each stage, with its reason
        assert any(e.what.endswith(f"-> {stage}") and e.why for e in entries if e.what.startswith("stage ")), stage
    decisions = [e for e in entries if e.kind == "policy"]
    assert decisions and all("level" in e.what and e.why.startswith("rule/reason:") for e in decisions)
    n_policy = conn.execute("SELECT count(*) FROM audit_log WHERE event = 'policy.decided' AND deal_id = %s",
                            (rig.deal_id,)).fetchone()[0]
    assert len(decisions) == n_policy
    actions = conn.execute("SELECT type, status, provider_id FROM actions WHERE deal_id = %s",
                           (rig.deal_id,)).fetchall()
    for kind, status, provider_id in actions:                        # each outbox action, with its status
        assert any(kind in e.what and f"[{status}]" in e.what and e.kind in {"outbox", "crm"}
                   for e in entries), (kind, status)
        if provider_id:
            assert provider_id in text
    sent = [e for e in entries if e.kind == "email-out"]
    received = [e for e in entries if e.kind == "email-in"]
    assert sent and "Message-ID <" in sent[0].why and received and "<in1@x>" in received[0].why
    assert "classified price_objection" in received[0].why
    assert any(e.kind == "agent" for e in entries) and any(e.kind == "proposal" for e in entries)
    assert any(e.kind == "crm" and "crm_update" in e.what for e in entries)
    assert entries == sorted(entries, key=lambda e: e.at)            # newest last


def test_the_timeline_never_contains_a_message_body(conn, ro_conn, pack, clock):
    rig = full_slice(conn, ro_conn, pack, clock)
    body = conn.execute("SELECT body FROM messages WHERE direction = 'inbound'").fetchone()[0]
    text = "\n".join(e.line() for e in timeline(conn, str(rig.deal_id)))
    assert body and body not in text and "CFO reviews" not in text      # no body; only the evidence quote
    assert '"Your pricing is too expensive for us."' in text


def test_lessons_used_and_held_replies_appear_with_their_ids(conn, ro_conn, pack, clock):
    rig = full_slice(conn, ro_conn, pack, clock)
    lesson = conn.execute("INSERT INTO lessons (text, evidence, tags, source, confidence) VALUES "
                          "('x', 'y', '{}', 'call', 0.5) RETURNING id").fetchone()[0]
    conn.execute("INSERT INTO lesson_uses (lesson_id, deal_id) VALUES (%s, %s)", (lesson, rig.deal_id))
    conn.execute("INSERT INTO held_replies (message_id, from_addr, subject, reason, candidate_deal_ids) "
                 "VALUES ('<h@x>', 'a@b', 'which deal?', 'ambiguous', %s)", ([rig.deal_id],))
    kinds = {e.kind: e for e in timeline(conn, str(rig.deal_id))}
    assert f"L{lesson}" in kinds["lesson"].what and "<h@x>" in kinds["held"].why


def test_a_lead_is_found_by_email_and_a_deal_with_no_history_has_an_empty_timeline(conn, ro_conn, pack, clock):
    rig = make_rig(conn, ro_conn, pack, clock)
    assert [str(d["id"]) for d in find_deals(conn, "SAM.LEE@brightside-optical.example")] == [str(rig.deal_id)]
    assert find_deals(conn, "nobody@x.org") == [] and timeline(conn, str(rig.deal_id)) == []


@pytest.fixture
def cli(test_db_url, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", test_db_url)
    return CliRunner()


def test_the_cli_prints_the_timeline_json_and_finds_by_lead(conn, ro_conn, pack, clock, cli):
    rig = full_slice(conn, ro_conn, pack, clock)
    plain = cli.invoke(app, ["trace", str(rig.deal_id)])
    assert plain.exit_code == 0 and "stage scored -> outreach" in plain.output and "policy" in plain.output
    as_json = cli.invoke(app, ["trace", "--lead", "sam.lee@brightside-optical.example", "--json"])
    data = json.loads(as_json.output)
    assert list(data) == [str(rig.deal_id)] and {"at", "kind", "what", "why"} <= set(data[str(rig.deal_id)][0])
    missing = cli.invoke(app, ["trace", "--lead", "nobody@x.org"])
    assert missing.exit_code == 1
    recent = cli.invoke(app, ["trace", "--recent", "--json"])
    assert recent.exit_code == 0 and "deal.stage_changed" in [a["event"] for a in json.loads(recent.output)["audit"]]
    assert cli.invoke(app, ["trace", "--recent"]).exit_code == 0
