import re
from pathlib import Path

from typer.testing import CliRunner

from revos.cli.main import app
from revos.demo import run_demo

ROOT = Path(__file__).resolve().parents[2]


def test_the_demo_runs_from_arrival_to_handoff_offline(conn, test_db_url):
    lines: list[str] = []
    report = run_demo(test_db_url, out=lines.append)
    text = "\n".join(lines)

    assert report.final_stage == "handoff" and report.runner == "rules"
    assert report.stages[-3:] == ["out_for_signature", "closed_won", "handoff"]
    assert (report.emails, report.invites, report.handoffs) == (4, 1, 1)
    assert "Runner for every agent: rules" in text and "runner=rules" in text
    assert "policy.decided" in text and "lesson.cited" in text and "OFFLINE DEMO" in text
    assert "forged webhook: rejected" in text and "same webhook again: duplicate" in text
    assert "TEST MODE" in text and "not real: no real email" in text
    assert text.count("handoff posted to #revops (once)") == 1
    # the lesson from the call changes the angle, and the lesson is named
    assert report.angle_before != report.angle_after and report.lesson_id is not None
    assert f"angle WITH the lesson:    {report.angle_after}   (cites lesson L{report.lesson_id})" in text
    assert f"angle WITHOUT the lesson: {report.angle_before}" in text
    assert re.search(r"lesson.outcome\s+L\d+ -> confidence 0\.60", text)


def test_make_demo_is_wired_to_the_command(conn):
    makefile = (ROOT / "Makefile").read_text()
    assert re.search(r"^demo: db-up\n\t\$\(VENV\)/bin/revos demo$", makefile, re.MULTILINE)


def test_the_demo_command_runs_in_its_own_database(conn):
    result = CliRunner().invoke(app, ["demo"])
    assert result.exit_code == 0, result.output
    assert "=== Summary" in result.output and "closed_won -> handoff" in result.output
