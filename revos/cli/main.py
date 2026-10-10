import typer
from rich.console import Console
from rich.table import Table

from revos.adapters.clock import SystemClock
from revos.db import connect
from revos.runtime.queue import list_dead_letters, retry_dead_letter

app = typer.Typer(help="Agentic Revenue OS command line.")
queue_app = typer.Typer(help="Inspect and repair the job queue.")
app.add_typer(queue_app, name="queue")


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
