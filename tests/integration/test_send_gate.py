import threading
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from revos.adapters.mock_provider import MockActionProvider
from revos.domain.enums import AutonomyMode, Tier
from revos.policy.config import PolicyConfig
from revos.policy.gate import send_with_gate
from revos.policy.service import decide_by_human, propose_action
from revos.policy.settings import set_autonomy_mode, set_kill_switch
from tests.helpers import CFG, good_first_touch, make_contact, propose, status


def sent_count(conn) -> int:
    return conn.execute("SELECT count(*) FROM actions WHERE status = 'sent'").fetchone()[0]


# 5. the gate re-reads the decision --------------------------------------------------------

def test_caller_flags_in_the_payload_do_not_approve_anything(conn, clock) -> None:
    c = make_contact(conn)
    p = propose_action(conn, clock, CFG, key="k", contact_id=c,
                       request=good_first_touch(body="our price?", rate_card_ok=False),
                       payload={"approved": True, "decision": "approved", "status": "approved"})
    provider = MockActionProvider()
    result = send_with_gate(conn, p.action_id, provider, clock, CFG)
    assert result.status == "refused" and "not approved" in result.reason
    assert provider.performed == [] and provider.find_calls == []


def test_veto_pending_action_is_not_sent_before_its_window_ends(conn, clock) -> None:
    p = propose(conn, clock, "k", good_first_touch(tier=Tier.A), make_contact(conn))
    provider = MockActionProvider()
    assert send_with_gate(conn, p.action_id, provider, clock, CFG).status == "refused"
    assert provider.performed == []


def test_decision_revoked_in_the_database_stops_the_send(conn, clock) -> None:
    p = propose(conn, clock, "k", good_first_touch(), make_contact(conn))
    conn.execute("UPDATE actions SET status = 'cancelled', decision = 'vetoed' WHERE id = %s",
                 (p.action_id,))
    provider = MockActionProvider()
    assert send_with_gate(conn, p.action_id, provider, clock, CFG).status == "refused"
    assert provider.performed == []


def test_human_approval_in_the_database_lets_it_through(conn, clock) -> None:
    p = propose(conn, clock, "k", good_first_touch(body="the price?", rate_card_ok=False),
                make_contact(conn))
    provider = MockActionProvider()
    assert send_with_gate(conn, p.action_id, provider, clock, CFG).status == "refused"
    decide_by_human(conn, clock, p.action_id, "manager@example.test", True)
    result = send_with_gate(conn, p.action_id, provider, clock, CFG)
    assert result.status == "sent" and provider.performed == ["k"]
    assert send_with_gate(conn, p.action_id, provider, clock, CFG).status == "sent"   # idempotent
    assert provider.performed == ["k"]


def test_send_records_sent_at_and_clears_hold(conn, clock) -> None:
    p = propose(conn, clock, "k", good_first_touch(), make_contact(conn))
    send_with_gate(conn, p.action_id, MockActionProvider(), clock, CFG)
    row = conn.execute("SELECT sent_at, hold_reason FROM actions WHERE id = %s", (p.action_id,)).fetchone()
    assert row == (clock.now(), None)


# 4. hard blocks never send, in either mode ---------------------------------------------------

@pytest.mark.parametrize("mode", [AutonomyMode.AUTONOMOUS, AutonomyMode.APPROVAL])
def test_hard_blocks_never_send(conn, clock, mode) -> None:
    set_autonomy_mode(conn, mode, "test")
    provider = MockActionProvider()
    clean, suppressed = make_contact(conn), make_contact(conn, "other@example.test", suppressed=True)
    cases = {
        "claim": (replace(good_first_touch(), claim_check_passed=False), clean),
        "injection": (good_first_touch(injection_flagged=True), clean),
        "discount": (ActionRequest_io(discount=40), clean),
        "suppressed": (good_first_touch(), suppressed),
    }
    for name, (request, contact) in cases.items():
        p = propose(conn, clock, name, request, contact)
        assert send_with_gate(conn, p.action_id, provider, clock, CFG).status == "refused", name
    # kill switch: even an approved, clean action is held
    ok = propose(conn, clock, "ks", good_first_touch(), clean)
    set_kill_switch(conn, True, "test")
    assert send_with_gate(conn, ok.action_id, provider, clock, CFG).status == "wait"
    assert provider.performed == [] and sent_count(conn) == 0


def ActionRequest_io(discount):
    from revos.policy.engine import ActionRequest
    return ActionRequest("send_io", io_validated=True, discount_pct=discount)


def test_contact_suppressed_after_approval_is_caught_at_the_gate(conn, clock) -> None:
    c = make_contact(conn)
    p = propose(conn, clock, "k", good_first_touch(), c)
    conn.execute("UPDATE contacts SET suppressed = true, suppressed_at = now() WHERE id = %s", (c,))
    provider = MockActionProvider()
    result = send_with_gate(conn, p.action_id, provider, clock, CFG)
    assert (result.status, result.reason) == ("refused", "suppressed")
    assert provider.performed == [] and status(conn, p.action_id)[0] == "cancelled"


# 8. demo-mode recipient allowlist ---------------------------------------------------------------

def test_only_allowed_recipients_can_be_emailed_in_demo_mode(conn, clock) -> None:
    stranger = make_contact(conn, "ceo@realcompany.com")
    allowed = make_contact(conn, "Owner@Example.test")           # case-insensitive match
    provider = MockActionProvider()
    p1 = propose(conn, clock, "k1", good_first_touch(), stranger)
    p2 = propose(conn, clock, "k2", good_first_touch(), allowed)
    r1 = send_with_gate(conn, p1.action_id, provider, clock, CFG)
    r2 = send_with_gate(conn, p2.action_id, provider, clock, CFG)
    assert (r1.status, r1.reason) == ("refused", "recipient_not_allowed")
    assert r2.status == "sent"
    assert provider.performed == ["k2"]
    assert status(conn, p1.action_id)[0] == "cancelled"


def test_empty_allowlist_in_demo_mode_blocks_everyone(conn, clock) -> None:
    config = PolicyConfig()                                      # demo_mode, no allowed addresses
    p = propose(conn, clock, "k", good_first_touch(), make_contact(conn), config=config)
    assert send_with_gate(conn, p.action_id, MockActionProvider(), clock, config).status == "refused"


def test_outside_demo_mode_the_allowlist_is_not_applied(conn, clock) -> None:
    config = replace(CFG, demo_mode=False)
    p = propose(conn, clock, "k", good_first_touch(), make_contact(conn, "anyone@realcompany.com"),
                config=config)
    assert send_with_gate(conn, p.action_id, MockActionProvider(), clock, config).status == "sent"


# 7. send windows in the prospect's time zone ---------------------------------------------------

def test_window_uses_the_prospects_time_zone(conn, clock) -> None:
    provider = MockActionProvider()
    cases = {          # clock is Monday 09:00 UTC
        "Europe/London": "sent",           # 10:00 local, open
        "Asia/Tokyo": "deferred",          # 18:00 local, closed
        "America/New_York": "deferred",    # 05:00 local, closed
    }
    ids = {}
    for i, tz in enumerate(cases):
        contact = make_contact(conn, f"p{i}@example.test", tz=tz)
        ids[tz] = propose(conn, clock, f"k{i}", good_first_touch(), contact,
                          config=replace(CFG, demo_mode=False)).action_id
    for tz, expected in cases.items():
        result = send_with_gate(conn, ids[tz], provider, clock, replace(CFG, demo_mode=False))
        assert result.status == expected, tz
    tokyo = send_with_gate(conn, ids["Asia/Tokyo"], provider, clock, replace(CFG, demo_mode=False))
    assert tokyo.run_at == datetime(2026, 10, 13, 0, 0, tzinfo=UTC)          # 09:00 JST tomorrow
    assert status(conn, ids["Asia/Tokyo"])[0] == "approved"                  # deferred, not dropped


def test_deferred_send_goes_out_when_the_window_opens(conn, clock) -> None:
    cfg = replace(CFG, demo_mode=False)
    p = propose(conn, clock, "k", good_first_touch(), make_contact(conn, tz="Asia/Tokyo"), config=cfg)
    provider = MockActionProvider()
    first = send_with_gate(conn, p.action_id, provider, clock, cfg)
    assert first.status == "deferred"
    clock.advance(3600)
    assert send_with_gate(conn, p.action_id, provider, clock, cfg).status == "deferred"   # not due yet
    clock.advance(15 * 3600)                                                  # past 00:00 UTC
    assert send_with_gate(conn, p.action_id, provider, clock, cfg).status == "sent"
    assert provider.performed == ["k"]
    assert conn.execute("SELECT count(*) FROM audit_log WHERE event = 'gate.held'").fetchone()[0] == 1


# 6. caps: atomic, deferred not dropped -----------------------------------------------------------

def _two_senders(test_db_url, clock, config, ids, provider):
    import psycopg
    results = [None, None]
    barrier = threading.Barrier(2)

    def run(slot: int) -> None:
        with psycopg.connect(test_db_url, autocommit=True) as c:
            barrier.wait()
            results[slot] = send_with_gate(c, ids[slot], provider, clock, config)

    threads = [threading.Thread(target=run, args=(i,)) for i in range(2)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    return results


@pytest.mark.parametrize("cap_field,emails", [
    ("daily_cap", ("a@shared.test", "other@example.test")),
    ("domain_cap", ("a@shared.test", "b@shared.test")),
])
def test_two_concurrent_sends_at_the_limit_exactly_one_passes(
    conn, clock, test_db_url, cap_field, emails
) -> None:
    config = replace(CFG, **{cap_field: 1})
    ids = [propose(conn, clock, f"k{i}", good_first_touch(), make_contact(conn, e), config=config).action_id
           for i, e in enumerate(emails)]
    provider = MockActionProvider()
    results = _two_senders(test_db_url, clock, config, ids, provider)
    assert sorted(r.status for r in results) == ["deferred", "sent"]
    assert len(provider.performed) == 1
    deferred = next(r for r in results if r.status == "deferred")
    assert deferred.reason.startswith("cap:")
    assert deferred.run_at == datetime(2026, 10, 13, 9, 0, tzinfo=UTC)       # next day, window open


def test_over_cap_send_is_deferred_not_dropped_and_goes_out_next_day(conn, clock) -> None:
    config = replace(CFG, daily_cap=2)
    ids = [propose(conn, clock, f"k{i}", good_first_touch(), make_contact(conn, e), config=config).action_id
           for i, e in enumerate(["owner@example.test", "sender@example.test", "other@example.test"])]
    provider = MockActionProvider()
    statuses = [send_with_gate(conn, i, provider, clock, config).status for i in ids]
    assert statuses == ["sent", "sent", "deferred"]
    assert status(conn, ids[2])[0] == "approved"                              # still queued
    assert conn.execute("SELECT count FROM send_counters WHERE scope = 'daily'").fetchone()[0] == 2
    clock.advance(24 * 3600)
    assert send_with_gate(conn, ids[2], provider, clock, config).status == "sent"
    assert len(provider.performed) == 3


def test_domain_slot_is_given_back_when_the_daily_cap_blocks(conn, clock) -> None:
    config = replace(CFG, daily_cap=1, domain_cap=5)
    a = propose(conn, clock, "a", good_first_touch(), make_contact(conn, "a@shared.test"), config=config)
    b = propose(conn, clock, "b", good_first_touch(), make_contact(conn, "b@shared.test"), config=config)
    provider = MockActionProvider()
    assert send_with_gate(conn, a.action_id, provider, clock, config).status == "sent"
    assert send_with_gate(conn, b.action_id, provider, clock, config).status == "deferred"
    assert conn.execute("SELECT count FROM send_counters WHERE scope = 'domain:shared.test'").fetchone()[0] == 1


def test_failed_send_gives_its_cap_slot_back(conn, clock) -> None:
    config = replace(CFG, daily_cap=1)
    p = propose(conn, clock, "k", good_first_touch(), make_contact(conn), config=config)
    flaky = MockActionProvider(crash_after_perform=True)
    with pytest.raises(ConnectionError):
        send_with_gate(conn, p.action_id, flaky, clock, config)
    assert conn.execute("SELECT count(*) FROM send_counters").fetchone()[0] == 0
    assert send_with_gate(conn, p.action_id, flaky, clock, config).status == "sent"
    assert flaky.performed == ["k"]                                           # provider check, no resend


# 9. kill switch: wait, then resume with nothing lost ------------------------------------------

def test_kill_switch_pauses_and_resumes_without_losing_actions(conn, clock) -> None:
    config = replace(CFG, daily_cap=100, domain_cap=100)
    ids = [propose(conn, clock, f"k{i}", good_first_touch(), make_contact(conn, f"p{i}@example.test"),
                   config=replace(config, demo_mode=False)).action_id for i in range(6)]
    config = replace(config, demo_mode=False)
    provider = MockActionProvider()

    set_kill_switch(conn, True, "owner")
    assert [send_with_gate(conn, i, provider, clock, config).status for i in ids] == ["wait"] * 6
    assert provider.performed == [] and sent_count(conn) == 0
    assert conn.execute("SELECT count(*) FROM actions WHERE status = 'approved'").fetchone()[0] == 6
    assert conn.execute("SELECT count(*) FROM actions WHERE hold_reason = 'kill_switch'").fetchone()[0] == 6

    set_kill_switch(conn, False, "owner")
    assert [send_with_gate(conn, i, provider, clock, config).status for i in ids] == ["sent"] * 6
    assert sorted(provider.performed) == [f"k{i}" for i in range(6)]
    assert conn.execute("SELECT count(*) FROM actions WHERE hold_reason IS NOT NULL").fetchone()[0] == 0
    events = [r[0] for r in conn.execute("SELECT event FROM audit_log WHERE event LIKE 'system.%' ORDER BY id")]
    assert events == ["system.kill_switch", "system.kill_switch"]


def test_kill_switch_flipped_after_approval_still_stops_the_send(conn, clock) -> None:
    p = propose(conn, clock, "k", good_first_touch(), make_contact(conn))        # approved while switch off
    set_kill_switch(conn, True, "owner")                                          # flipped before the send
    provider = MockActionProvider()
    assert send_with_gate(conn, p.action_id, provider, clock, CFG).status == "wait"
    assert provider.performed == []
