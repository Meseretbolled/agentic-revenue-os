"""DOMAIN_CAP_MODE: enforce holds the fourth send to one domain; demo_bypass sends it and audits it,
and the daily cap, allowlist, suppression, kill switch and the send window are untouched."""

from datetime import UTC, datetime

import pytest
from typer.testing import CliRunner

from revos.cli.main import app
from revos.policy.config import ConfigError, PolicyConfig, domain_cap_bypassed
from tests.pipeline_helpers import CONTACT_EMAIL, audit, make_lead, make_rig

DOMAIN = CONTACT_EMAIL.rsplit("@", 1)[1]


def fill_domain(conn, clock, used):
    conn.execute("INSERT INTO send_counters (scope, day, count) VALUES (%s, %s, %s)",
                 (f"domain:{DOMAIN}", clock.now().date(), used))


def first(conn, ro_conn, pack, clock, **kw):
    rig = make_rig(conn, ro_conn, pack, clock, **kw)
    return rig, rig.orch.handle_lead_created(rig.deal_id)


def cap(pack):
    return pack.policy.domain_cap


def test_enforce_holds_a_send_once_the_domain_cap_is_used(conn, ro_conn, pack, clock):
    rig, f = first(conn, ro_conn, pack, clock)
    fill_domain(conn, clock, cap(pack))
    result = rig.orch.send_action(f.action_id)
    assert result.status == "deferred" and result.reason == f"cap:domain:{DOMAIN}"
    assert audit(conn, "domain_cap.bypassed") == []


def test_bypass_sends_it_and_writes_an_audit_row_with_the_reason(conn, ro_conn, pack, clock):
    rig, f = first(conn, ro_conn, pack, clock, domain_cap_bypass=True)
    fill_domain(conn, clock, cap(pack))
    assert rig.orch.send_action(f.action_id).status == "sent"
    (row,) = audit(conn, "domain_cap.bypassed", rig.deal_id)
    assert row["action_id"] == str(f.action_id) and row["domain"] == DOMAIN
    assert f"per-domain cap of {cap(pack)}" in row["reason"] and "demo bypass" in row["reason"]


def test_a_send_under_the_domain_cap_is_not_audited_as_bypassed(conn, ro_conn, pack, clock):
    rig, f = first(conn, ro_conn, pack, clock, domain_cap_bypass=True)
    assert rig.orch.send_action(f.action_id).status == "sent"
    assert audit(conn, "domain_cap.bypassed") == []


def test_bypass_never_relaxes_the_overall_daily_cap(conn, ro_conn, pack, clock):
    rig, f = first(conn, ro_conn, pack, clock, domain_cap_bypass=True, daily_cap=1)
    fill_domain(conn, clock, cap(pack))
    conn.execute("INSERT INTO send_counters (scope, day, count) VALUES ('daily', %s, 1)", (clock.now().date(),))
    result = rig.orch.send_action(f.action_id)
    assert result.status == "deferred" and result.reason == "cap:daily"
    assert audit(conn, "domain_cap.bypassed") == []                  # it did not send, so nothing bypassed
    assert conn.execute("SELECT count FROM send_counters WHERE scope = %s",
                        (f"domain:{DOMAIN}",)).fetchone()[0] == cap(pack)      # and no slot was consumed


def test_bypass_never_relaxes_the_allowlist(conn, ro_conn, pack, clock):
    rig, f = first(conn, ro_conn, pack, clock, domain_cap_bypass=True)
    fill_domain(conn, clock, cap(pack))
    conn.execute("UPDATE contacts SET email = 'stranger@elsewhere.org'")
    result = rig.orch.send_action(f.action_id)
    assert result.status == "refused" and result.reason == "recipient_not_allowed"


def test_bypass_never_relaxes_suppression(conn, ro_conn, pack, clock):
    rig, f = first(conn, ro_conn, pack, clock, domain_cap_bypass=True)
    fill_domain(conn, clock, cap(pack))
    conn.execute("UPDATE contacts SET suppressed = true, suppressed_at = now()")
    assert rig.orch.send_action(f.action_id).reason == "suppressed"


def test_bypass_never_relaxes_the_kill_switch(conn, ro_conn, pack, clock):
    rig, f = first(conn, ro_conn, pack, clock, domain_cap_bypass=True)
    fill_domain(conn, clock, cap(pack))
    conn.execute("UPDATE settings SET value = 'true' WHERE key = 'kill_switch'")
    assert rig.orch.send_action(f.action_id).status == "wait"
    assert audit(conn, "domain_cap.bypassed") == []


def test_bypass_does_not_relax_the_send_window(conn, ro_conn, pack, clock):
    clock.set(datetime(2026, 10, 11, 9, 0, tzinfo=UTC))                # a Sunday
    rig, f = first(conn, ro_conn, pack, clock, domain_cap_bypass=True)
    result = rig.orch.send_action(f.action_id)
    assert result.status == "deferred" and result.reason == "send_window"


def test_the_two_bypasses_are_independent(conn, ro_conn, pack, clock):
    clock.set(datetime(2026, 10, 11, 9, 0, tzinfo=UTC))
    rig, f = first(conn, ro_conn, pack, clock, send_window_bypass=True)
    fill_domain(conn, clock, cap(pack))
    result = rig.orch.send_action(f.action_id)                         # window bypassed, domain cap enforced
    assert result.status == "deferred" and result.reason.startswith("cap:domain")
    assert audit(conn, "domain_cap.bypassed") == []


def test_other_domains_keep_their_own_counters(conn, ro_conn, pack, clock):
    rig, f = first(conn, ro_conn, pack, clock, domain_cap_bypass=True, allowed=("a@other.org",))
    fill_domain(conn, clock, cap(pack))
    other, _ = make_lead(conn, email="a@other.org", name="Al")
    g = rig.orch.handle_lead_created(other)
    assert rig.orch.send_action(g.action_id).status == "sent"
    assert audit(conn, "domain_cap.bypassed") == []


def test_bypass_never_takes_effect_outside_demo_mode(pack):
    assert PolicyConfig.from_pack(pack, demo_mode=False, domain_cap_bypass=True).domain_cap_bypass is False


@pytest.mark.parametrize("env", [{"REVOS_ENV": "dev"}, {}, {"REVOS_ENV": "production"}])
def test_bypass_outside_the_demo_environment_refuses_to_start(env):
    with pytest.raises(ConfigError, match="refusing to start"):
        domain_cap_bypassed({"DOMAIN_CAP_MODE": "demo_bypass", **env})


def test_the_modes_parse_strictly():
    assert domain_cap_bypassed({}) is False and domain_cap_bypassed({"DOMAIN_CAP_MODE": "enforce"}) is False
    assert domain_cap_bypassed({"DOMAIN_CAP_MODE": " Demo_Bypass ", "REVOS_ENV": "demo"}) is True
    with pytest.raises(ConfigError, match="must be"):
        domain_cap_bypassed({"DOMAIN_CAP_MODE": "off"})


def test_from_env_and_the_composition_root_refuse_too(conn, ro_conn, monkeypatch):
    from revos.composition import build_orchestrator
    monkeypatch.setenv("DOMAIN_CAP_MODE", "demo_bypass")
    monkeypatch.setenv("REVOS_ENV", "dev")
    with pytest.raises(ConfigError):
        PolicyConfig.from_env()
    with pytest.raises(ConfigError):
        build_orchestrator(conn, ro_conn)
    monkeypatch.setenv("REVOS_ENV", "demo")
    assert PolicyConfig.from_env().domain_cap_bypass is True
    assert PolicyConfig.from_env().send_window_bypass is False


def test_run_and_doctor_refuse_and_say_plainly(monkeypatch, tmp_path, test_db_url):
    from revos import doctor
    from revos.doctor import OK, Check
    monkeypatch.setenv("DATABASE_URL", test_db_url)
    monkeypatch.setenv("REVOS_LOG_FILE", str(tmp_path / "l.jsonl"))
    monkeypatch.setattr(doctor, "run_checks", lambda: [Check("Postgres", OK)])
    monkeypatch.setenv("DOMAIN_CAP_MODE", "demo_bypass")
    monkeypatch.setenv("REVOS_ENV", "dev")
    for command in (["run", "--no-api"], ["doctor"]):
        result = CliRunner().invoke(app, command)
        assert result.exit_code == 1 and "refusing to start" in result.output
    monkeypatch.setenv("REVOS_ENV", "demo")
    out = CliRunner().invoke(app, ["doctor"]).output
    assert "DOMAIN CAP BYPASSED: demo only (all demo mail goes to one domain)" in out
    assert "SEND WINDOW BYPASSED" not in out and "enforced (the pack's weekday hours)" in out
    monkeypatch.setenv("DOMAIN_CAP_MODE", "enforce")
    assert "enforced (the pack's per-domain cap)" in CliRunner().invoke(app, ["doctor"]).output


def test_the_gate_itself_ignores_the_flag_outside_demo_mode(conn, ro_conn, pack, clock):
    from dataclasses import replace

    from revos.policy.gate import send_with_gate
    rig, f = first(conn, ro_conn, pack, clock)
    fill_domain(conn, clock, cap(pack))
    config = replace(rig.orch.policy_config(pack), demo_mode=False, domain_cap_bypass=True)   # inconsistent
    result = send_with_gate(conn, f.action_id, rig.orch.provider, clock, config)
    assert result.status == "deferred" and result.reason.startswith("cap:domain")


def test_retry_held_sends_the_held_email_through_the_normal_gate_and_never_twice(conn, ro_conn, pack, clock):
    import random

    from revos.policy.settings import retry_held_sends
    from revos.runtime.worker import Worker
    clock.set(datetime(2026, 10, 11, 9, 0, tzinfo=UTC))                        # a Sunday
    rig, f = first(conn, ro_conn, pack, clock)
    fill_domain(conn, clock, cap(pack))
    assert rig.orch.send_action(f.action_id).status == "deferred"             # held, with a timer
    rig.orch.send_window_bypass = rig.orch.domain_cap_bypass = True           # restarted in demo mode
    assert retry_held_sends(conn, clock, "operator", rig.deal_id) == [f.action_id]
    assert audit(conn, "operator.retry_held", rig.deal_id)
    worker = Worker(rig.orch.handlers(), clock, rng=random.Random(1))
    while worker.run_once(conn):
        pass
    assert conn.execute("SELECT status FROM actions WHERE id = %s", (f.action_id,)).fetchone()[0] == "sent"
    assert audit(conn, "domain_cap.bypassed", rig.deal_id) and audit(conn, "send_window.bypassed", rig.deal_id)
    assert retry_held_sends(conn, clock, "operator") == []                       # nothing held any more
    assert conn.execute("SELECT count(*) FROM messages WHERE direction = 'outbound'").fetchone()[0] == 1


def test_retry_held_is_just_another_attempt_and_every_check_still_applies(conn, ro_conn, pack, clock):
    from revos.policy.settings import retry_held_sends
    rig, f = first(conn, ro_conn, pack, clock)
    fill_domain(conn, clock, cap(pack))
    assert rig.orch.send_action(f.action_id).status == "deferred"
    retry_held_sends(conn, clock, "operator")
    assert rig.orch.send_action(f.action_id).reason == f"cap:domain:{DOMAIN}"    # held again, not bypassed
    conn.execute("UPDATE settings SET value = 'true' WHERE key = 'kill_switch'")
    assert rig.orch.send_action(f.action_id).status == "wait"
    assert retry_held_sends(conn, clock, "operator") == []                       # a kill-switch hold is not its job
