import typer
from rich.console import Console
from rich.table import Table

from revos.adapters.clock import SystemClock
from revos.db import connect
from revos.policy.settings import resume_after_kill_switch, set_kill_switch
from revos.runtime.queue import list_dead_letters, retry_dead_letter
from revos.segments import PackError, load_all

app = typer.Typer(help="Agentic Revenue OS command line.")
queue_app = typer.Typer(help="Inspect and repair the job queue.")
app.add_typer(queue_app, name="queue")


@app.command("packs")
def packs(packs_dir: str = typer.Argument("packs")) -> None:
    """Validate every segment pack; exits 1 and names the file and field on the first problem."""
    try:
        loaded = load_all(packs_dir)
    except PackError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    for name, pack in loaded.items():
        typer.echo(f"ok  {name} v{pack.meta.version}: {len(pack.case_studies)} case studies")


@app.command("kill-switch")
def kill_switch(state: str = typer.Argument(..., help="on or off")) -> None:
    """Pause all outbound sending (on), or resume it and re-queue everything that waited (off)."""
    if state not in {"on", "off"}:
        typer.echo("state must be 'on' or 'off'", err=True)
        raise typer.Exit(2)
    with connect() as conn:
        conn.autocommit = True
        if state == "on":
            set_kill_switch(conn, True, "operator")
            typer.echo("kill switch ON: outbound actions will wait")
        else:
            released = resume_after_kill_switch(conn, SystemClock(), "operator")
            typer.echo(f"kill switch OFF: {len(released)} held send(s) re-queued")



@app.command("demo")
def demo() -> None:
    """Run one fictional lead from arrival to handoff, offline, narrated."""
    from revos.demo import demo_main
    demo_main()


@app.command("doctor")
def doctor() -> None:
    """One status per service: REAL OK, REAL FAILED (with a reason category) or MOCK. No values."""
    from revos.doctor import FAILED, run_checks
    from revos.env import load_env_file
    load_env_file()
    checks = run_checks()
    for check in checks:
        typer.echo(check.line())
    if any(c.status == FAILED for c in checks):
        raise typer.Exit(1)


def _hubspot_client():
    import os

    from revos.adapters.hubspot.client import DEFAULT_BASE_URL, HubSpotClient
    from revos.env import load_env_file
    load_env_file()
    token = os.environ.get("HUBSPOT_TOKEN", "").strip()
    if not token:
        typer.echo("HUBSPOT_TOKEN is not set: nothing to do (HubSpot stays on the mock)", err=True)
        raise typer.Exit(2)
    return HubSpotClient(token, os.environ.get("HUBSPOT_BASE_URL", "").strip() or DEFAULT_BASE_URL)


@app.command("hubspot-setup")
def hubspot_setup() -> None:
    """Create the custom properties revos needs in HubSpot. Safe to run again: it changes nothing."""
    from revos.adapters.hubspot.client import HubSpotError, HubSpotUnavailable
    from revos.adapters.hubspot.schema import ensure_properties
    try:
        report = ensure_properties(_hubspot_client())
    except (HubSpotError, HubSpotUnavailable) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    typer.echo(f"created {len(report.created)} propert(ies), {len(report.existing)} already there"
               + (f", property group added for {report.groups_created}" if report.groups_created else ""))
    if report.stages_missing:
        typer.echo("deal stages our map uses that this pipeline lacks: " + ", ".join(report.stages_missing))


@app.command("reset-demo")
def reset_demo_command(
    hubspot: bool = typer.Option(False, "--hubspot", help="delete the HubSpot demo records"),
    yes: bool = typer.Option(False, "--yes", help="do not ask first"),
    dry_run: bool = typer.Option(False, "--dry-run", help="only count"),
) -> None:
    """Delete ONLY records flagged revos_demo=true. Local reset arrives with the dashboard step."""
    if not hubspot:
        typer.echo("nothing selected: use --hubspot (the local reset comes in a later step)", err=True)
        raise typer.Exit(2)
    from revos.adapters.hubspot.client import HubSpotError, HubSpotUnavailable
    from revos.adapters.hubspot.reset import find_demo_records, reset_demo
    from revos.db import connect
    client = _hubspot_client()
    try:
        found = find_demo_records(client)
        total = sum(len(v) for v in found.values())
        typer.echo("flagged revos_demo=true: " + ", ".join(f"{len(v)} {k}" for k, v in found.items()))
        if dry_run or not total:
            return
        if not yes and not typer.confirm(f"delete these {total} record(s) from HubSpot?"):
            raise typer.Exit(1)
        with connect() as conn:
            notes = [r[0] for r in conn.execute("SELECT provider_id FROM actions WHERE type = 'crm_note' "
                                                "AND status = 'sent' AND provider_id IS NOT NULL")]
        counts = reset_demo(client, extra_note_ids=notes)
    except (HubSpotError, HubSpotUnavailable) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    typer.echo("deleted: " + ", ".join(f"{n} {k}" for k, n in counts.items()))


@app.callback()
def root() -> None:
    """Agentic Revenue OS command line."""


@queue_app.command("dead-letters")
def dead_letters() -> None:
    """List jobs that failed for good."""
    with connect() as conn:
        rows = list_dead_letters(conn)
    table = Table("id", "job", "type", "retried", "error")
    for r in rows:
        table.add_row(str(r["id"]), str(r["job_id"]), r["type"], "yes" if r["retried_at"] else "no",
                      r["error"] or "")
    Console().print(table)


@queue_app.command("retry")
def retry(dead_letter_id: int) -> None:
    """Put a dead-lettered job back on the queue."""
    with connect() as conn:
        try:
            job_id = retry_dead_letter(conn, dead_letter_id, SystemClock())
        except (LookupError, ValueError) as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(1) from exc
    typer.echo(f"job {job_id} re-queued")


if __name__ == "__main__":
    app()
