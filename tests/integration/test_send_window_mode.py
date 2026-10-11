"""SEND_WINDOW_MODE: enforce holds a Sunday send; demo_bypass sends it, audits it, and nothing else
is relaxed."""

from datetime import UTC, datetime

import pytest
from typer.testing import CliRunner

from revos.cli.main import app
from revos.policy.config import ConfigError, PolicyConfig, send_window_bypassed
from tests.pipeline_helpers import audit, make_rig

SUNDAY = datetime(2026, 10, 11, 9, 0, tzinfo=UTC)


@pytest.fixture
def sunday(clock):
    clock.set(SUNDAY)
    return clock


def first_touch(conn, ro_conn, pack, clock, **kw):
    rig = make_rig(conn, ro_conn, pack, clock, **kw)
    return rig, rig.orch.handle_lead_created(rig.deal_id)


def test_enforce_holds_a_sunday_send_until_monday(conn, ro_conn, pack, sunday):
    rig, first = first_touch(conn, ro_conn, pack, sunday)
    result = rig.orch.send_action(first.action_id)
    assert result.status == "deferred" and result.reason == "send_window"
    assert result.run_at == datetime(2026, 10, 12, 9, 0, tzinfo=UTC)
    assert conn.execute("SELECT status FROM actions WHERE id = %s", (first.action_id,)).fetchone()[0] \
        == "approved"
    assert audit(conn, "send_window.bypassed") == []


def test_bypass_sends_it_and_writes_an_audit_row_with_the_reason(conn, ro_conn, pack, sunday):
    rig, first = first_touch(conn, ro_conn, pack, sunday, send_window_bypass=True)
    assert rig.orch.send_action(first.action_id).status == "sent"
    (row,) = audit(conn, "send_window.bypassed", rig.deal_id)
    assert row["action_id"] == str(first.action_id)
    assert "demo bypass" in row["reason"] and "2026-10-12T09:00:00" in row["reason"]


def test_a_send_inside_the_window_is_not_audited_as_bypassed(conn, ro_conn, pack, clock):
    rig, first = first_touch(conn, ro_conn, pack, clock, send_window_bypass=True)     # Monday 09:00
    assert rig.orch.send_action(first.action_id).status == "sent"
    assert audit(conn, "send_window.bypassed") == []


def test_bypass_does_not_relax_the_allowlist(conn, ro_conn, pack, sunday):
    rig, first = first_touch(conn, ro_conn, pack, sunday, send_window_bypass=True)
    conn.execute("UPDATE contacts SET email = 'stranger@elsewhere.org'")
    result = rig.orch.send_action(first.action_id)
    assert result.status == "refused" and result.reason == "recipient_not_allowed"
    assert audit(conn, "send_window.bypassed") == []


def test_bypass_does_not_relax_suppression(conn, ro_conn, pack, sunday):
    rig, first = first_touch(conn, ro_conn, pack, sunday, send_window_bypass=True)
    conn.execute("UPDATE contacts SET suppressed = true, suppressed_at = now()")
    assert rig.orch.send_action(first.action_id).reason == "suppressed"


def test_bypass_does_not_relax_the_kill_switch(conn, ro_conn, pack, sunday):
    rig, first = first_touch(conn, ro_conn, pack, sunday, send_window_bypass=True)
    conn.execute("UPDATE system_settings SET value = 'true' WHERE key = 'kill_switch'")
    assert rig.orch.send_action(first.action_id).status == "wait"
    assert audit(conn, "send_window.bypassed") == []


def test_bypass_does_not_relax_the_daily_cap(conn, ro_conn, pack, sunday):
    rig, first = first_touch(conn, ro_conn, pack, sunday, send_window_bypass=True, daily_cap=1)
    conn.execute("INSERT INTO send_counters (scope, day, count) VALUES ('daily', %s, 1)", (SUNDAY.date(),))
    result = rig.orch.send_action(first.action_id)
    assert result.status == "deferred" and result.reason == "cap:daily"


def test_bypass_never_takes_effect_outside_demo_mode(pack):
    config = PolicyConfig.from_pack(pack, demo_mode=False, send_window_bypass=True)
    assert config.send_window_bypass is False


@pytest.mark.parametrize("env", [{"REVOS_ENV": "dev"}, {}, {"REVOS_ENV": "production"}, {"REVOS_ENV": ""}])
def test_bypass_outside_the_demo_environment_refuses_to_start(env):
    with pytest.raises(ConfigError, match="refusing to start"):
        send_window_bypassed({"SEND_WINDOW_MODE": "demo_bypass", **env})


def test_the_modes_parse_strictly():
    assert send_window_bypassed({}) is False and send_window_bypassed({"SEND_WINDOW_MODE": "enforce"}) is False
    assert send_window_bypassed({"SEND_WINDOW_MODE": " Demo_Bypass ", "REVOS_ENV": "demo"}) is True
    with pytest.raises(ConfigError, match="must be"):
        send_window_bypassed({"SEND_WINDOW_MODE": "off"})


def test_from_env_and_the_composition_root_refuse_too(conn, ro_conn, monkeypatch):
    from revos.composition import build_orchestrator
    monkeypatch.setenv("SEND_WINDOW_MODE", "demo_bypass")
    monkeypatch.setenv("REVOS_ENV", "dev")
    with pytest.raises(ConfigError):
        PolicyConfig.from_env()
    with pytest.raises(ConfigError):
        build_orchestrator(conn, ro_conn)
    monkeypatch.setenv("REVOS_ENV", "demo")
    assert PolicyConfig.from_env().send_window_bypass is True


def test_run_and_doctor_refuse_to_start_and_say_plainly_when_it_is_on(monkeypatch, tmp_path, test_db_url):
    from revos import doctor
    from revos.doctor import OK, Check
    monkeypatch.setenv("DATABASE_URL", test_db_url)
    monkeypatch.setenv("REVOS_LOG_FILE", str(tmp_path / "l.jsonl"))
    monkeypatch.setattr(doctor, "run_checks", lambda: [Check("Postgres", OK)])
    monkeypatch.setenv("SEND_WINDOW_MODE", "demo_bypass")
    monkeypatch.setenv("REVOS_ENV", "dev")
    for command in (["run", "--no-api"], ["doctor"]):
        result = CliRunner().invoke(app, command)
        assert result.exit_code == 1 and "refusing to start" in result.output
    monkeypatch.setenv("REVOS_ENV", "demo")
    assert "SEND WINDOW BYPASSED: demo only" in CliRunner().invoke(app, ["doctor"]).output
    monkeypatch.setenv("SEND_WINDOW_MODE", "enforce")
    assert "enforced" in CliRunner().invoke(app, ["doctor"]).output
