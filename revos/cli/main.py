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
