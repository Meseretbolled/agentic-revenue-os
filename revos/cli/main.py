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
    """One status per service: REAL OK, REAL FAILED (with a reason category) or MOCK, and what it
    will run as. No values."""
    from revos.doctor import FAILED, run_checks
    from revos.env import load_env_file
    from revos.modes import integration_modes
    from revos.policy.config import (
        BYPASS_BANNER,
        DOMAIN_CAP_BANNER,
        ConfigError,
        domain_cap_bypassed,
        send_window_bypassed,
    )
    load_env_file()
    try:
        bypass, cap_bypass = send_window_bypassed(), domain_cap_bypassed()
    except ConfigError as exc:
        typer.echo(f"CONFIG ERROR  {exc}", err=True)
        raise typer.Exit(1) from exc
    checks = run_checks()
    modes = {m.service: m for m in integration_modes()}
    for check in checks:
        mode = modes[check.service]
        if mode.built:
            typer.echo(f"{check.line():<50} will run as: {mode.mode}")
        else:
            typer.echo(mode.line_with(check.status, check.detail))
    typer.echo(f"{'Send window':<14}" + (BYPASS_BANNER if bypass else "enforced (the pack's weekday hours)"))
    typer.echo(f"{'Domain cap':<14}"
               + (DOMAIN_CAP_BANNER if cap_bypass else "enforced (the pack's per-domain cap)"))
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
    hubspot: bool = typer.Option(False, "--hubspot", help="also delete the HubSpot demo records"),
    yes: bool = typer.Option(False, "--yes", help="do not ask first"),
    dry_run: bool = typer.Option(False, "--dry-run", help="only count"),
) -> None:
    """Cancel queued demo work locally (unsent emails, queued jobs) and, with --hubspot, delete ONLY
    the HubSpot records flagged revos_demo=true."""
    from revos.db import connect, migrate
    from revos.demo_reset import cancel_pending_work
    from revos.env import load_env_file
    load_env_file()
    if not dry_run and not yes and not typer.confirm("cancel all unsent actions and queued jobs?"):
        raise typer.Exit(1)
    with connect() as conn:
        conn.autocommit = True
        migrate(conn)
        if dry_run:
            left = conn.execute("SELECT count(*) FROM actions WHERE status IN "
                                "('pending', 'awaiting_approval', 'approved')").fetchone()[0]
            typer.echo(f"local: {left} unsent action(s) would be cancelled")
        else:
            counts = cancel_pending_work(conn)
            typer.echo(f"local: cancelled {counts['actions']} unsent action(s) and "
                       f"{counts['jobs']} queued job(s)")
    if not hubspot:
        return
    from revos.adapters.hubspot.client import HubSpotError, HubSpotUnavailable
    from revos.adapters.hubspot.reset import find_demo_records, reset_demo
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


@app.command("trace")
def trace(
    deal_id: str = typer.Argument(None, help="the deal to trace"),
    lead: str = typer.Option(None, "--lead", help="find the deal(s) by the contact's email"),
    recent: bool = typer.Option(False, "--recent", help="recent audit entries and held replies"),
    as_json: bool = typer.Option(False, "--json", help="machine-readable output"),
    limit: int = typer.Option(40, help="how many audit entries with --recent"),
) -> None:
    """One deal's timeline (newest last): events, agent runs, policy decisions, outbox and CRM
    actions, emails, held replies, lessons and audit rows, each with what happened and why."""
    import json

    from revos.db import migrate
    from revos.trace import audit_trail, find_deals, held_replies, timeline
    with connect() as conn:
        migrate(conn)
        if recent or not (deal_id or lead):
            entries, held = audit_trail(conn, None, limit), held_replies(conn)
            if as_json:
                typer.echo(json.dumps({"audit": entries, "held_replies": held}, default=str, indent=2))
                return
            table = Table("at", "actor", "event", "deal", "detail")
            for e in entries:
                table.add_row(e["at"].strftime("%m-%d %H:%M:%S"), e["actor"], e["event"],
                              str(e["deal_id"] or "")[:8], str(e["detail"])[:80])
            console = Console()
            console.print(table)
            if held:
                console.print(f"[bold]{len(held)} reply(ies) held: more than one deal could own them[/bold]")
                for h in held:
                    console.print(f"  {h['message_id']} from {h['from_addr']}: "
                                  f"{(h['subject'] or '')[:60]!r} candidates: "
                                  f"{', '.join(str(c)[:8] for c in h['candidate_deal_ids'])}")
            return
        ids = [deal_id] if deal_id else [str(d["id"]) for d in find_deals(conn, lead)]
        if not ids:
            typer.echo(f"no deal found for {lead}", err=True)
            raise typer.Exit(1)
        result = {i: [e.as_dict() for e in timeline(conn, i)] for i in ids}
        if as_json:
            typer.echo(json.dumps(result, indent=2, default=str))
            return
        for i in ids:
            typer.echo(f"Deal {i}")
            for e in timeline(conn, i):
                typer.echo(e.line())


@app.command("logs")
def logs(deal: str = typer.Option(None, "--deal", help="only this deal id"),
         correlation: str = typer.Option(None, "--correlation", help="only this correlation id"),
         tail: int = typer.Option(50, help="how many lines"),
         path: str = typer.Option(None, help="log file (default var/logs/revos.jsonl)")) -> None:
    """Print structured log lines from var/logs/revos.jsonl, optionally for one deal."""
    import json

    from revos.obs import read_log
    for row in read_log(path, deal_id=deal, correlation_id=correlation, limit=tail):
        typer.echo(json.dumps(row, ensure_ascii=False))


@app.command("slack-test")
def slack_test() -> None:
    """Post ONE test message (with a harmless button) to SLACK_CHANNEL, then update it in place."""
    import os

    from revos.composition import build_slack
    from revos.domain.errors import RevosError
    from revos.env import load_env_file
    from revos.modes import slack_configured
    load_env_file()
    if not slack_configured():
        typer.echo("SLACK_BOT_TOKEN is not set: Slack runs as MOCK. Nothing was posted.", err=True)
        raise typer.Exit(2)
    slack, channel = build_slack()

    def note(text: str) -> dict:
        return {"type": "context", "elements": [{"type": "mrkdwn", "text": text}]}

    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text":
               ":white_check_mark: *Revos is connected.* This is a test message; nothing is queued."}},
              note("Posted by `revos slack-test`.")]
    try:
        posted = slack.post(channel, "Revos is connected", blocks)
        slack.update(posted.channel, posted.ts, "Revos is connected (updated in place)",
                     [*blocks, note("Updated in place with chat.update.")])
    except RevosError as exc:
        typer.echo(f"{exc}", err=True)
        raise typer.Exit(1) from exc
    has_secret = bool(os.environ.get("SLACK_SIGNING_SECRET", "").strip())
    has_url = bool(os.environ.get("PUBLIC_BASE_URL", "").strip())
    typer.echo(f"posted to {channel} (channel id {posted.channel}) and updated it in place")
    typer.echo("SLACK_SIGNING_SECRET: " + ("set" if has_secret else "NOT set (buttons will be refused)"))
    typer.echo("PUBLIC_BASE_URL: " + ("set" if has_url else "NOT set (buttons will not work)"))


@app.command("run")
def run(
    port: int = typer.Option(8100, help="API port"),
    no_api: bool = typer.Option(False, "--no-api", help="run only the worker and the scheduler"),
    force: bool = typer.Option(False, "--force", help="start even if a REAL service fails its check"),
) -> None:
    """Start the API, the worker and the scheduler together. Ctrl-C stops them cleanly."""
    import os
    import signal
    import threading

    from revos.adapters.clock import SystemClock
    from revos.composition import build_orchestrator
    from revos.db import database_url, migrate
    from revos.doctor import FAILED, run_checks
    from revos.env import load_env_file
    from revos.modes import REAL, integration_modes
    from revos.obs import configure_logging, log
    from revos.policy.config import (
        BYPASS_BANNER,
        DOMAIN_CAP_BANNER,
        ConfigError,
        domain_cap_bypassed,
        send_window_bypassed,
    )
    from revos.runtime.supervisor import Supervisor

    load_env_file()
    try:
        bypass, cap_bypass = send_window_bypassed(), domain_cap_bypassed()
    except ConfigError as exc:
        typer.echo(f"CONFIG ERROR  {exc}", err=True)
        raise typer.Exit(1) from exc
    log_file = configure_logging()
    modes = integration_modes()
    results = {c.service: c for c in run_checks()}
    typer.echo("revos run: Agentic Revenue OS")
    for mode in modes:
        check = results.get(mode.service)
        typer.echo("  " + mode.line_with(check.status if check else None, check.detail if check else ""))
    typer.echo(f"  API           {'off' if no_api else f'http://127.0.0.1:{port}'}   logs: {log_file}")
    if bypass:
        typer.echo(f"  !! {BYPASS_BANNER}. Allowlist, caps, suppression and the kill switch still apply.")
        log("run", BYPASS_BANNER, level="warning")
    if cap_bypass:
        typer.echo(f"  !! {DOMAIN_CAP_BANNER}. The daily cap, allowlist, suppression and kill "
                   "switch still apply.")
        log("run", DOMAIN_CAP_BANNER, level="warning")

    # The tunnel points at the API this command is about to start, so it cannot pass beforehand: it is
    # re-checked once the API is up (and only ever costs the Slack buttons, never safety).
    failed = [c for c in results.values() if c.status == FAILED and c.service != "Tunnel URL"
              and any(m.service == c.service and m.mode == REAL for m in modes)]
    for check in failed:
        log("run", f"{check.service} will run as REAL but its check failed", level="error",
            detail=check.detail)
        typer.echo(f"  FAILED  {check.line()}", err=True)
    if failed and not force:
        typer.echo("A service that will run as REAL is not working. Fix it (revos doctor) or pass --force.\n"
                   "The app never swaps in a mock quietly.", err=True)
        raise typer.Exit(1)

    with connect() as conn:
        migrate(conn)
    supervisor = Supervisor(
        database_url=database_url(), clock=SystemClock(), api_port=None if no_api else port,
        build_handlers=lambda conn, ro, clock: build_orchestrator(conn, ro, clock).handlers())
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    supervisor.start()
    typer.echo("running. Ctrl-C to stop.")
    if any(m.service == "Tunnel URL" and m.mode == REAL for m in modes):
        import time

        import httpx

        from revos.doctor import check_tunnel
        time.sleep(2)                                   # let the API come up
        tunnel = check_tunnel(dict(os.environ), httpx.Client())
        typer.echo(f"  {tunnel.line()}" + ("" if tunnel.status == "REAL OK"
                                           else "  (Slack buttons will not work until it answers)"))
        if tunnel.status == FAILED:
            log("run", "the tunnel does not answer /healthz", level="warning", detail=tunnel.detail)
    while not stop.is_set() and not supervisor.stop_event.is_set():
        stop.wait(0.5)
    typer.echo("stopping: finishing the current job...")
    report = supervisor.stop()
    typer.echo("stopped cleanly" if report.finished_cleanly else
               f"stopped; released job(s) {report.released_job_ids} back to the queue")
    if supervisor.errors:
        typer.echo("; ".join(supervisor.errors), err=True)
        raise typer.Exit(1)


@app.command("seed")
def seed(
    tag: str = typer.Argument("acme", help="makes the lead name+tag@<your prospect inbox>"),
    profile: str = typer.Option(None, "--profile", help="hot, warm or cold: lead data from the fixture"),
    company: str = typer.Option("Brightside Optical Group", help="a fictional company"),
    domain: str = typer.Option("brightside-optical.example", help="its (fictional) web domain"),
    contact: str = typer.Option("Sam Lee", help="a fictional contact name"),
    segment: str = typer.Option(None, help="the segment pack (default: the demo pack)"),
) -> None:
    """Create one fictional lead addressed to a plus-address of PROSPECT_TEST_EMAIL and queue it."""
    from pathlib import Path

    from revos.adapters.clock import SystemClock
    from revos.db import migrate
    from revos.env import load_env_file
    from revos.seed import demo_address
    from revos.seed import seed_lead as seed_one
    from revos.segments import load_all

    load_env_file()
    packs = load_all(Path(__file__).resolve().parent.parent.parent / "packs")
    if segment is None:
        segment = next((p.id for p in packs.values() if p.meta.demo), None)
    if segment not in packs:
        typer.echo(f"no segment pack {segment!r}; have: {', '.join(sorted(packs))}", err=True)
        raise typer.Exit(1)
    email = demo_address(tag)
    fields: dict | None = None
    timezone: str | None = None
    if profile:
        from revos.seed import PROFILE_NAMES, load_profiles
        if profile not in PROFILE_NAMES:
            typer.echo(f"--profile must be one of {', '.join(PROFILE_NAMES)}", err=True)
            raise typer.Exit(1)
        chosen = load_profiles(segment)[profile]
        company, domain, contact = chosen["company"], chosen["domain"], chosen["contact"]
        fields = {**chosen["fields"], **({"research_notes": chosen["research_notes"]}
                                         if chosen.get("research_notes") else {})}
        timezone = chosen.get("timezone")
    with connect() as conn:
        conn.autocommit = True
        migrate(conn)
        deal_id, created = seed_one(conn, SystemClock(), segment_id=segment,
                                    pack_version=str(packs[segment].meta.version), company=company,
                                    domain=domain, contact_name=contact, email=email, fields=fields,
                                    timezone=timezone)
    typer.echo(f"{'seeded' if created else 'already seeded'}: deal {deal_id} for {email}")


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


@queue_app.command("retry-held")
def retry_held(deal: str = typer.Option(None, "--deal", help="only this deal id")) -> None:
    """Try sends held by the send window or a cap again now. They pass the full gate again."""
    from uuid import UUID

    from revos.policy.settings import retry_held_sends
    with connect() as conn:
        conn.autocommit = True
        queued = retry_held_sends(conn, SystemClock(), "operator", UUID(deal) if deal else None)
    typer.echo(f"queued {len(queued)} held send(s) for another attempt")


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
