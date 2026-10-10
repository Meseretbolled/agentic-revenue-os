from dataclasses import replace
from datetime import timedelta

import pytest

from revos.domain.enums import AutonomyMode, Tier
from revos.policy.config import OUTBOUND_TYPES, PolicyConfig
from revos.policy.engine import ActionRequest
from revos.policy.service import (
    decide_by_human,
    release_veto_expired,
    veto_action,
)
from revos.policy.settings import set_autonomy_mode
from tests.helpers import CFG, good_first_touch, make_contact, propose, status


def test_auto_action_is_stored_approved_by_policy(conn, clock) -> None:
    c = make_contact(conn)
    p = propose(conn, clock, "k1", good_first_touch(), c)
    assert status(conn, p.action_id)[:2] == ("approved", "approved")
    assert conn.execute("SELECT decision_by FROM actions").fetchone()[0] == "policy"


def test_same_key_twice_is_one_action(conn, clock) -> None:
    c = make_contact(conn)
    a = propose(conn, clock, "k1", good_first_touch(), c)
    b = propose(conn, clock, "k1", good_first_touch(), c)
    assert (a.created, b.created) == (True, False) and a.action_id == b.action_id


def test_every_decision_is_audited_with_its_reasons(conn, clock) -> None:
    propose(conn, clock, "k1", good_first_touch(tier=Tier.A), make_contact(conn))
    detail = conn.execute("SELECT detail FROM audit_log WHERE event = 'policy.decided'").fetchone()[0]
    assert detail["outcome"] == "auto_with_veto" and detail["mode"] == "autonomous"
    assert any("tier" in r or "first_touch.A" in r for r in detail["reasons"])


def test_flipping_mode_in_the_database_changes_the_stored_outcome(conn, clock) -> None:
    request = good_first_touch(tier=Tier.A)
    first = propose(conn, clock, "k1", request, make_contact(conn, "a@shared.test"))
    set_autonomy_mode(conn, AutonomyMode.APPROVAL, "owner")
    second = propose(conn, clock, "k2", request, make_contact(conn, "b@shared.test"))
    assert status(conn, first.action_id)[0] == "pending"             # veto window
    assert status(conn, second.action_id)[0] == "awaiting_approval"  # a person decides
    set_autonomy_mode(conn, AutonomyMode.AUTONOMOUS, "owner")
    third = propose(conn, clock, "k3", request, make_contact(conn, "c@shared.test"))
    assert status(conn, third.action_id)[0] == "pending"


def test_unknown_action_is_stored_cancelled_and_denied(conn, clock) -> None:
    p = propose(conn, clock, "k1", ActionRequest("wire_transfer"))
    assert status(conn, p.action_id)[:2] == ("cancelled", "rejected")


def test_suppressed_contact_is_read_from_the_database(conn, clock) -> None:
    c = make_contact(conn, suppressed=True)
    p = propose(conn, clock, "k1", good_first_touch(), c)      # caller does not say "suppressed"
    assert status(conn, p.action_id)[:2] == ("cancelled", "rejected")
    assert "suppressed" in p.decision.reasons


def test_social_message_is_only_ever_a_draft(conn, clock) -> None:
    p = propose(conn, clock, "k1", ActionRequest("social_message"))
    assert status(conn, p.action_id)[0] == "draft"


def test_veto_window_expires_into_approval(conn, clock) -> None:
    p = propose(conn, clock, "k1", good_first_touch(tier=Tier.A), make_contact(conn))
    assert release_veto_expired(conn, clock) == []
    clock.advance(CFG.veto_window_seconds)
    assert release_veto_expired(conn, clock) == [p.action_id]
    assert status(conn, p.action_id)[:2] == ("approved", "approved")
    assert conn.execute("SELECT decision_by FROM actions").fetchone()[0] == "policy:veto_window"
    assert release_veto_expired(conn, clock) == []              # exactly once


def test_veto_inside_the_window_cancels_and_late_veto_is_refused(conn, clock) -> None:
    c = make_contact(conn)
    p = propose(conn, clock, "k1", good_first_touch(tier=Tier.A), c)
    clock.advance(30)
    veto_action(conn, clock, p.action_id, "owner")
    assert status(conn, p.action_id)[:2] == ("cancelled", "vetoed")
    q = propose(conn, clock, "k2", good_first_touch(tier=Tier.A), make_contact(conn, "other@example.test"))
    clock.advance(61)
    with pytest.raises(ValueError):
        veto_action(conn, clock, q.action_id, "owner")


def test_human_approval_records_who_and_when(conn, clock) -> None:
    c = make_contact(conn)
    p = propose(conn, clock, "k1", good_first_touch(body="What about the price?", rate_card_ok=False), c)
    assert status(conn, p.action_id)[0] == "awaiting_approval"
    decide_by_human(conn, clock, p.action_id, "manager@example.test", True, "ok")
    assert status(conn, p.action_id)[:2] == ("approved", "approved")
    assert conn.execute("SELECT decided_by FROM approvals").fetchone()[0] == "manager@example.test"
    with pytest.raises(ValueError):
        decide_by_human(conn, clock, p.action_id, "x", True)    # cannot decide twice


def test_a_hard_blocked_action_cannot_be_approved_by_a_person(conn, clock) -> None:
    p = propose(conn, clock, "k1", replace(good_first_touch(), claim_check_passed=False), make_contact(conn))
    with pytest.raises(ValueError):
        decide_by_human(conn, clock, p.action_id, "manager@example.test", True)


@pytest.mark.parametrize("kind", sorted(OUTBOUND_TYPES))
def test_database_guards_cover_every_outbound_type(conn, kind) -> None:
    """Keeps PolicyConfig.OUTBOUND_TYPES and the SQL type lists in sync."""
    import psycopg
    c = make_contact(conn, suppressed=True)
    with pytest.raises(psycopg.errors.RaiseException, match="suppressed"):
        conn.execute("INSERT INTO actions (idempotency_key, contact_id, type, autonomy_level) "
                     "VALUES ('x', %s, %s, 'auto')", (c, kind))
    with pytest.raises(psycopg.errors.CheckViolation, match="email_requires_contact"):
        conn.execute("INSERT INTO actions (idempotency_key, type, autonomy_level) "
                     "VALUES ('y', %s, 'auto')", (kind,))


def test_config_from_env_builds_the_demo_allowlist(monkeypatch) -> None:
    monkeypatch.setenv("REVOS_ENV", "demo")
    monkeypatch.setenv("PROSPECT_TEST_EMAIL", "Prospect@Example.test ")
    monkeypatch.setenv("GMAIL_ADDRESS", "sender@example.test")
    config = PolicyConfig.from_env()
    assert config.demo_mode and config.allowed_recipients == {"prospect@example.test", "sender@example.test"}
    assert timedelta(seconds=config.veto_window_seconds) == timedelta(seconds=60)
