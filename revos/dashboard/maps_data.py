"""What the live maps read. Plain SELECTs against the live database and the code's own constants (the agent
registry, the scheduler's schedules, the packs' stage files); nothing here writes, and nothing is invented.

* `architecture()`: the system map (integrations, runtime, agents, policy, send gate, stages), live.
* `data_model()`: every table in the real schema, built from the database catalogue so it cannot drift."""

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

from revos.agents.registry import AGENTS
from revos.domain.stages import DealStage
from revos.runtime.scheduler import Schedule

# The main path, in the order a deal moves through it (closed_lost and do_not_contact are side exits).
MAIN_PATH = [s.value for s in DealStage if s not in (DealStage.CLOSED_LOST, DealStage.DO_NOT_CONTACT)]
POLLS = {"email.poll": "Gmail", "calls.poll": "Fireflies"}
POLL_OF = {service: event for event, service in POLLS.items()}

# How the tables are grouped on the data map. Presentation only; a table not listed here lands under "Other",
# so a new table still shows up.
AREAS: dict[str, tuple[str, ...]] = {
    "Deals and people": ("segments", "companies", "contacts", "deals", "documents", "transcripts"),
    "Messaging": ("messages", "held_replies", "held_calls", "slack_messages", "slack_interactions"),
    "Actions and approvals": ("actions", "approvals", "esign_attempts", "send_counters"),
    "Runtime": ("events", "jobs", "dead_letters", "system_settings", "schema_migrations"),
    "Agents": ("agent_runs", "agent_steps", "agent_proposals", "llm_spend"),
    "Knowledge": ("lessons", "lesson_uses", "case_studies", "case_study_sends"),
    "Audit": ("audit_log",),
}


def _hour_counts(cur: Any, now: datetime) -> dict[str, int]:
    since = now - timedelta(hours=1)
    one = lambda query: cur.execute(query, (since,)).fetchone()["n"]  # noqa: E731
    return {
        "events": one("SELECT count(*) AS n FROM events WHERE received_at > %s"),
        "runs": one("SELECT count(*) AS n FROM agent_runs WHERE started_at > %s"),
        "decisions": one("SELECT count(*) AS n FROM audit_log WHERE actor LIKE 'policy%%' AND at > %s"),
        "sent": one("SELECT count(*) AS n FROM actions WHERE sent_at > %s"),
    }


def _agents(cur: Any, now: datetime) -> list[dict[str, Any]]:
    last = {r["agent"]: r for r in cur.execute(
        "SELECT DISTINCT ON (agent) agent, runner, started_at, status FROM agent_runs "
        "ORDER BY agent, started_at DESC").fetchall()}
    hour = {r["agent"]: r["n"] for r in cur.execute(
        "SELECT agent, count(*) AS n FROM agent_runs WHERE started_at > %s GROUP BY agent",
        (now - timedelta(hours=1),)).fetchall()}
    out = []
    for spec in AGENTS.values():
        run = last.get(spec.name)
        out.append({"name": spec.name, "kind": spec.kind, "does": spec.description,
                    "last_run": run["started_at"].isoformat() if run else None,
                    "runner": run["runner"] if run else None, "status": run["status"] if run else None,
                    "runs_1h": hour.get(spec.name, 0)})
    return out


def _gate(cur: Any, now: datetime, caps: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    counters = cur.execute("SELECT scope, count FROM send_counters WHERE day = %s ORDER BY scope",
                           (now.date(),)).fetchall()
    bypass = {r["event"]: r["n"] for r in cur.execute(
        "SELECT event, count(*) AS n FROM audit_log "
        "WHERE event IN ('send_window.bypassed', 'domain_cap.bypassed') GROUP BY event").fetchall()}
    return {"day": now.date().isoformat(),
            "daily_used": next((r["count"] for r in counters if r["scope"] == "daily"), 0),
            "domains": [{"domain": r["scope"].removeprefix("domain:"), "used": r["count"]}
                        for r in counters if r["scope"].startswith("domain:")],
            "caps": list(caps),
            "bypass_send_window": bypass.get("send_window.bypassed", 0),
            "bypass_domain_cap": bypass.get("domain_cap.bypassed", 0)}


def _stages(cur: Any, criteria: Mapping[str, Mapping[str, Sequence[str]]]) -> list[dict[str, Any]]:
    counts = {r["stage"]: r["n"]
              for r in cur.execute("SELECT stage, count(*) AS n FROM deals GROUP BY stage")}
    out = []
    for stage in MAIN_PATH:
        by_pack = criteria.get(stage, {})
        distinct = {tuple(v) for v in by_pack.values()}
        if len(distinct) <= 1:      # the same in every pack: say it once
            first = list(next(iter(distinct), ()))
            needs: list[dict[str, Any]] = [{"pack": None, "fields": first}] if distinct else []
        else:
            needs = [{"pack": p, "fields": list(v)} for p, v in sorted(by_pack.items())]
        out.append({"stage": stage, "n": counts.get(stage, 0), "needs": needs})
    return out


def architecture(conn: psycopg.Connection, now: datetime, *, integrations: Sequence[Mapping[str, Any]],
                 controls: Mapping[str, Any], schedules: Sequence[Schedule],
                 caps: Sequence[Mapping[str, Any]],
                 criteria: Mapping[str, Mapping[str, Sequence[str]]]) -> dict[str, Any]:
    cur = conn.cursor(row_factory=dict_row)
    jobs = {r["status"]: r["n"]
            for r in cur.execute("SELECT status, count(*) AS n FROM jobs GROUP BY status")}
    dead_open = cur.execute("SELECT count(*) AS n FROM dead_letters WHERE retried_at IS NULL "
                            "AND acknowledged_at IS NULL").fetchone()["n"]
    polled = {r["type"]: r["last"] for r in cur.execute(
        "SELECT type, max(created_at) AS last FROM jobs WHERE type = ANY(%s) AND status = 'done' "
        "GROUP BY type",
        (list(POLLS),)).fetchall()}
    return {
        "as_of": now.isoformat(),
        "integrations": [dict(row, polled=polled[POLL_OF[row["service"]]].isoformat()
                              if row["service"] in POLL_OF and POLL_OF[row["service"]] in polled else None)
                         for row in integrations],
        "runtime": {"jobs": jobs, "dead_open": dead_open,
                    "schedules": [{"event": s.event_type, "every_seconds": s.every_seconds}
                                  for s in schedules]},
        "agents": _agents(cur, now),
        "policy": {"kill_switch": bool(controls["kill_switch"].value),
                   "autonomy_mode": str(controls["autonomy_mode"].value),
                   "veto_seconds": int(controls["veto_window_seconds"].value)},
        "gate": _gate(cur, now, caps),
        "hour": _hour_counts(cur, now),
        "stages": _stages(cur, criteria),
    }


def _area(table: str) -> str:
    return next((area for area, names in AREAS.items() if table in names), "Other")


def data_model(conn: psycopg.Connection) -> dict[str, Any]:
    cur = conn.cursor(row_factory=dict_row)
    columns = cur.execute(
        "SELECT table_name, column_name, data_type, is_nullable FROM information_schema.columns "
        "WHERE table_schema = 'public' ORDER BY table_name, ordinal_position").fetchall()
    tables = [r["tablename"] for r in cur.execute(
        "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename").fetchall()]
    keys = cur.execute(
        "SELECT c.conrelid::regclass::text AS tbl, c.contype, c.conname, "
        "  (SELECT array_agg(a.attname ORDER BY k.ord) FROM unnest(c.conkey) WITH ORDINALITY k(attnum, ord) "
        "   JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum) AS cols, "
        "  c.confrelid::regclass::text AS ref "
        "FROM pg_constraint c JOIN pg_namespace n ON n.oid = c.connamespace "
        "WHERE n.nspname = 'public' AND c.contype IN ('p', 'f', 'c')").fetchall()
    triggers = cur.execute(
        "SELECT c.relname AS tbl, t.tgname AS name FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
        "JOIN pg_namespace n ON n.oid = c.relnamespace WHERE NOT t.tgisinternal AND n.nspname = 'public' "
        "ORDER BY 1, 2").fetchall()
    model: dict[str, dict[str, Any]] = {}
    for t in tables:
        n = cur.execute(sql.SQL("SELECT count(*) AS n FROM {}").format(sql.Identifier(t))).fetchone()["n"]
        model[t] = {"name": t, "area": _area(t), "rows": n, "columns": [], "pk": [], "checks": 0,
                    "triggers": []}
    for r in columns:
        if r["table_name"] in model:
            model[r["table_name"]]["columns"].append(
                {"name": r["column_name"], "type": r["data_type"], "null": r["is_nullable"] == "YES",
                 "fk": None})
    foreign: list[dict[str, Any]] = []
    for k in keys:
        t = model.get(k["tbl"])
        if t is None:
            continue
        if k["contype"] == "p":
            t["pk"] = list(k["cols"] or [])
        elif k["contype"] == "c":
            t["checks"] += 1
        elif k["contype"] == "f" and k["ref"] in model:
            foreign.append({"from": k["tbl"], "cols": list(k["cols"] or []), "to": k["ref"]})
            for col in t["columns"]:
                if col["name"] in (k["cols"] or []):
                    col["fk"] = k["ref"]
    for tr in triggers:
        if tr["tbl"] in model:
            model[tr["tbl"]]["triggers"].append(tr["name"])
    areas = [a for a in (*AREAS, "Other") if any(t["area"] == a for t in model.values())]
    return {"areas": areas, "tables": list(model.values()), "links": foreign}
