"""The orchestrator: runs agents, then decides what their proposals become.

The only code that turns a proposal into anything. For each proposal it recomputes what it can
from the database, asks the policy engine, creates the outbox action, and applies internal
effects. It is also the only caller of transition_deal for agent-driven moves.
"""

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, time, timedelta
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from revos.adapters.hubspot.crm import build_deal_properties
from revos.agents.broker import ToolBroker
from revos.agents.common import check_inputs, clean_fields, evidence_from_fields, lead_facts
from revos.agents.registry import AGENTS
from revos.agents.runner import AgentRunner
from revos.agents.store import RunRow, RunStore, canonical_hash
from revos.agents.types import AgentEnv, Identity
from revos.domain.drafting import Claim, Draft
from revos.domain.enums import Tier
from revos.domain.errors import (
    ExitCriteriaNotMet,
    LLMError,
    LoopLimitExceeded,
    ProposalRejected,
    QuarantinedContent,
    RevosError,
    SpendCapExceeded,
    ToolCallBlocked,
    WebhookRejected,
)
from revos.domain.quoted import new_text
from revos.domain.stages import DealStage, can_transition
from revos.domain.untrusted import UntrustedText
from revos.mcp.registry import BlockEvent, ToolContext
from revos.mcp.scope import fixed_scope
from revos.orchestrator import sweeps
from revos.orchestrator.transitions import transition_deal
from revos.policy.audit import write_audit
from revos.policy.config import PolicyConfig
from revos.policy.engine import ActionRequest
from revos.policy.gate import GateResult, run_approved_action
from revos.policy.recipients import base_of, recipient_allowed
from revos.policy.service import propose_action, release_veto_expired
from revos.policy.settings import release_held_sends, veto_window_override
from revos.ports.calendar import Calendar, Slot
from revos.ports.clock import Clock
from revos.ports.esign import ESign
from revos.ports.mail import Inbox
from revos.ports.provider import ActionProvider
from revos.ports.research import Research
from revos.ports.transcripts import Transcripts
from revos.runtime import lessons as lesson_loop
from revos.runtime.events import record_event
from revos.runtime.quarantine import record_quarantine
from revos.segments.loader import Pack
from revos.skills.call_matching import DealFacts, match_call
from revos.skills.claims import deterministic_check
from revos.skills.crm_fields import call_summary, outreach_status, sequence_day
from revos.skills.handoff import build_handoff
from revos.skills.io import validate_io
from revos.skills.io_terms import propose_io_terms
from revos.skills.scoring import score_lead
from revos.skills.slots import resolve_tz, slot_label
from revos.slack.notifier import POSTED_TYPES

# A run that ends one of these ways goes to a person, with an audit entry. Order matters.
HUMAN_CAUSES: tuple[tuple[type[Exception], str], ...] = (
    (SpendCapExceeded, "spend_cap_exceeded"), (QuarantinedContent, "quarantined"),
    (LoopLimitExceeded, "loop_limit"), (ToolCallBlocked, "tool_blocked"),
    (LLMError, "llm_error"), (ExitCriteriaNotMet, "exit_criteria_not_met"),
    (ProposalRejected, "proposal_rejected"),
)
INTERNAL = {"enrich", "score", "classify_reply", "suppress_contact", "cancel_scheduled_sends",
            "snooze_ooo", "reminder", "attach_case_study", "generate_io", "lesson_write"}
WEEKDAY_NAMES = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
PLUS_TAG = re.compile(r"\+([0-9a-f]{8})@")
CLOSED_STAGES = frozenset({"closed_won", "closed_lost", "do_not_contact", "handoff"})
SENDABLE = {"first_touch", "follow_up", "send_io", "crm_update", "crm_note", "book_meeting", "handoff"}
FIELD_PREFIXES = ("fact:", "fact_source:", "quote:")
EXTRA_FIELDS = {"company_domain", "decision_maker_name", "decision_maker_role", "signer_name",
                "signer_name_source", "signer_role"}
# What the IO is built from: a change to any of these is a new IO run.
IO_KEYS = ("io_items", "io_package", "io_start_date", "io_end_date", "discount_pct", "signer_email",
           "signer_name", "signer_role", "budget", "timeline", "decision_maker", "needs")
QUALIFICATION_KEYS = ("budget", "timeline", "decision_maker", "needs", "signer_name", "signer_role")
DEFAULT_SNOOZE_DAYS = 3        # no return date stated: look again after this long
MAX_SNOOZE_DAYS = 14           # a stated date further out is cut back to this
MAX_SNOOZES = 3                # after this many out-of-office replies a person looks


def cause_of(exc: Exception) -> str:
    return next((code for cls, code in HUMAN_CAUSES if isinstance(exc, cls)), "")


@dataclass(frozen=True)
class RunResult:
    run_id: UUID
    agent: str
    step: str
    runner: str
    status: str
    human_reason: str | None
    output: dict[str, Any]
    proposals: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class PipelineResult:
    status: str                      # action_proposed | closed | needs_human
    stage: str
    runner: str
    reason: str | None = None
    action_id: UUID | None = None
    decision: str | None = None
    label: str | None = None
    runs: tuple[tuple[str, str], ...] = ()


class Orchestrator:
    def __init__(self, conn: psycopg.Connection, ro_conn: psycopg.Connection, *, clock: Clock,
                 packs: Mapping[str, Pack], runner: AgentRunner, research: Research,
                 provider: ActionProvider, identity: Identity | None = None, demo_mode: bool = True,
                 allowed_recipients: frozenset[str] = frozenset(),
                 calendar: Calendar | None = None, transcripts: Transcripts | None = None,
                 learn_from_replies: bool = False, esign: ESign | None = None,
                 handoff_channel: str = "#revops",
                 customer_success_owner: str = "customer-success", inbox: Inbox | None = None,
                 crm_sync: bool = False, daily_cap: int | None = None,
                 plus_bases: frozenset[str] = frozenset(),
                 send_window_bypass: bool = False, domain_cap_bypass: bool = False,
                 notifier: Any = None, veto_window_seconds: int | None = None,
                 sender_email: str = "") -> None:
        self.conn, self.ro_conn, self.clock, self.packs = conn, ro_conn, clock, packs
        self.plus_bases, self.send_window_bypass = plus_bases, send_window_bypass
        self.domain_cap_bypass = domain_cap_bypass
        self.veto_window_seconds = veto_window_seconds   # None: the pack's own window
        self.notifier = notifier                        # SlackNotifier, or None when Slack is not wired
        self.runner, self.research, self.provider = runner, research, provider
        self.identity = identity or Identity()
        self.demo_mode, self.allowed_recipients, self.calendar = demo_mode, allowed_recipients, calendar
        self.transcripts, self.learn_from_replies = transcripts, learn_from_replies
        self.esign, self.handoff_channel = esign, handoff_channel
        self.customer_success_owner = customer_success_owner
        self.inbox, self.crm_sync, self.daily_cap = inbox, crm_sync, daily_cap
        self.sender_email = sender_email                # the From address shown on a logged outbound email
        self.store = RunStore(conn)

    def _audit_slots(self, deal_id: UUID, payload: Mapping[str, Any]) -> None:
        """Keep the UTC value of every offered slot on record, with how it was shown to the prospect."""
        write_audit(self.conn, "orchestrator", "slots.offered", deal_id, {
            "slots_utc": list(payload["slots"]), "timezone": payload.get("timezone") or "UTC",
            "shown_as": list(payload.get("slots_shown", []))})
        if not payload.get("timezone"):
            write_audit(self.conn, "orchestrator", "slots.timezone_unknown", deal_id, {
                "reason": "the contact has no (valid) time zone: slots were shown in UTC and said so"})

    def _slots(self, deal_id: UUID, reply_text: str = "") -> tuple[Slot, ...]:
        """Three free slots, judged in the prospect's own time zone: inside their working hours (the
        pack's send window hours) and, if the reply names a weekday ("can we talk Thursday?"), on that
        day. When nothing fits, fall back to the next free slots and say so in the audit."""
        if self.calendar is None:
            return ()
        deal = self._deal(deal_id)
        tz = resolve_tz(self.conn.execute("SELECT timezone FROM contacts WHERE id = %s",
                                          (deal["contact_id"],)).fetchone()[0])
        window = self.policy_config(self._pack(deal)).send_window
        now = self.clock.now()
        free = self.calendar.free_slots(now + timedelta(days=1), now + timedelta(days=8), 30)

        def local(slot: Slot) -> datetime:
            return slot.start.astimezone(tz) if tz else slot.start

        working = [s for s in free if tz is None
                   or window.start_hour <= local(s).hour and local(s).hour + local(s).minute / 60 + 0.5
                   <= window.end_hour]
        wanted = {i for i, name in enumerate(WEEKDAY_NAMES) if re.search(rf"\b{name}s?\b",
                                                                        new_text(reply_text), re.I)}
        preferred = [s for s in working if local(s).weekday() in wanted]
        if wanted and not preferred:
            write_audit(self.conn, "orchestrator", "slots.preference_unmet", deal_id,
                        {"reason": "no free slot on the weekday the reply asked for"})
        if not (preferred or working):
            write_audit(self.conn, "orchestrator", "slots.preference_unmet", deal_id,
                        {"reason": "no free slot inside the contact's working hours"})
        return tuple((preferred or working or free)[:3])

    def _last_outbound_subject(self, deal_id: UUID) -> str | None:
        row = self.conn.execute("SELECT subject FROM messages WHERE deal_id = %s AND direction = "
                                "'outbound' ORDER BY id DESC LIMIT 1", (deal_id,)).fetchone()
        return row[0] if row else None

    # --- reading ----------------------------------------------------------------------------------
    def _deal(self, deal_id: UUID) -> dict[str, Any]:
        row = self.conn.cursor(row_factory=dict_row).execute(
            "SELECT d.*, c.full_name AS contact_name, co.name AS company_name, co.domain "
            "FROM deals d LEFT JOIN contacts c ON c.id = d.contact_id "
            "LEFT JOIN companies co ON co.id = d.company_id WHERE d.id = %s", (deal_id,)).fetchone()
        if row is None:
            raise LookupError(f"no deal {deal_id}")
        return row

    def _pack(self, deal: Mapping[str, Any]) -> Pack:
        return self.packs[deal["segment_id"]]

    def policy_config(self, pack: Pack) -> PolicyConfig:
        config = PolicyConfig.from_pack(pack, demo_mode=self.demo_mode,
                                        allowed_recipients=self.allowed_recipients,
                                        plus_bases=self.plus_bases,
                                        send_window_bypass=self.send_window_bypass,
                                        domain_cap_bypass=self.domain_cap_bypass)
        if self.veto_window_seconds is not None:       # VETO_WINDOW_SECONDS, set deliberately
            config = replace(config, veto_window_seconds=self.veto_window_seconds)
        runtime = veto_window_override(self.conn)       # set at runtime: wins over the environment
        if runtime is not None:
            config = replace(config, veto_window_seconds=runtime)
        if self.daily_cap is not None:                 # a real mailbox gets a lower cap than the pack's
            config = replace(config, daily_cap=min(config.daily_cap, self.daily_cap))
        return config

    def _tool_ctx(self, deal_id: UUID) -> ToolContext:
        def blocked(event: BlockEvent) -> None:
            write_audit(self.conn, "tools", "tool.blocked", event.deal_id or deal_id,
                        {"agent": event.agent, "tool": event.tool, "reason": event.reason})

        def quarantined(content: UntrustedText, deal: UUID) -> None:
            record_quarantine(self.conn, content, deal)

        return ToolContext(self.ro_conn, self.packs, self.research, fixed_scope(self.ro_conn, deal_id),
                           blocked, quarantined)

    # --- running an agent -------------------------------------------------------------------------
    def run_agent(self, deal_id: UUID, agent: str, step: str, *, input_: Mapping[str, Any] | None = None,
                  untrusted: Mapping[str, UntrustedText] | None = None,
                  slots: tuple[Slot, ...] = ()) -> RunResult:
        """Run (or resume, or replay the record of) one agent step. Safe to call twice."""
        spec = AGENTS[agent]
        deal = self._deal(deal_id)
        pack = self._pack(deal)
        summary = {"input": dict(input_ or {}),
                   "untrusted": {k: {"origin": v.origin, "boundary": v.boundary}
                                 for k, v in (untrusted or {}).items()}}
        row = self.store.start(deal_id, agent, step, self.runner.name, summary)
        if row.status != "running":                               # a recorded result: not re-run
            return self._result(row)
        write_audit(self.conn, "orchestrator", "agent.run_started" if row.created
                    else "agent.run_resumed", deal_id,
                    {"agent": agent, "step": step, "runner": self.runner.name,
                     "kind": spec.kind, "recorded_steps": row.step_count})

        ctx = self._tool_ctx(deal_id)
        broker = ToolBroker(self.store, row.id, agent, ctx)
        env = AgentEnv(agent, step, self.runner.name, deal_id, deal["contact_id"], deal["company_id"],
                       pack, broker, self.clock, self.identity, dict(input_ or {}),
                       dict(untrusted or {}), slots)
        try:
            output = self.runner.run(spec, env, store=self.store, run_id=row.id, tool_ctx=ctx)
        except tuple(cls for cls, _ in HUMAN_CAUSES) as exc:
            if isinstance(exc, QuarantinedContent):
                for item in (untrusted or {}).values():
                    if item.flagged:
                        record_quarantine(self.conn, item, deal_id)
            return self._finish(row, deal_id, "needs_human", {"runner": self.runner.name,
                                "cause": cause_of(exc)}, f"{cause_of(exc)}: {exc}", ())
        info = {"runner": self.runner.name, "notes": output.notes,
                "stage_suggestion": output.stage_suggestion.value if output.stage_suggestion else None,
                "handoff": output.handoff,
                "tools_run": broker.executed, "tools_replayed": broker.replayed}
        if output.needs_human:
            return self._finish(row, deal_id, "needs_human", info, output.needs_human, ())
        return self._finish(row, deal_id, "completed", info, None, output.proposals)

    def _finish(self, row: RunRow, deal_id: UUID, status: str, info: dict[str, Any],
                reason: str | None, proposals) -> RunResult:
        self.store.finish(row.id, deal_id, status, info, reason, proposals)
        done = self.conn.cursor(row_factory=dict_row).execute(
            "SELECT * FROM agent_runs WHERE id = %s", (row.id,)).fetchone()
        return self._result(RunRow(done["id"], done["agent"], done["step"], done["runner"],
                                   done["status"], done["output"], done["human_reason"],
                                   done["step_count"], False))

    def _result(self, row: RunRow) -> RunResult:
        return RunResult(row.id, row.agent, row.step, row.runner, row.status, row.human_reason,
                         row.output or {}, self.store.proposals(row.id))

    # --- applying proposals -----------------------------------------------------------------------
    def apply_run(self, run: RunResult, deal_id: UUID) -> list[dict[str, Any]]:
        """Turn each pending proposal into a policy decision and its effect. Safe to call twice."""
        applied = []
        for proposal in self.store.proposals(run.run_id):
            if proposal["status"] != "pending":
                continue
            try:
                applied.append(self._apply_one(run, deal_id, proposal))
            except ProposalRejected as exc:
                self.conn.execute("UPDATE agent_proposals SET status = 'rejected', outcome = %s "
                                  "WHERE run_id = %s AND idx = %s",
                                  (f"rejected: {exc}", run.run_id, proposal["idx"]))
                write_audit(self.conn, "orchestrator", "agent.proposal_rejected", deal_id,
                            {"run_id": str(run.run_id), "idx": proposal["idx"],
                             "action_type": proposal["action_type"], "why": str(exc)})
                raise
        return applied

    def _apply_one(self, run: RunResult, deal_id: UUID, p: dict[str, Any]) -> dict[str, Any]:
        deal = self._deal(deal_id)
        pack = self._pack(deal)
        payload = dict(p["payload"])
        contact_id = payload.pop("_contact_id", None)
        atype = p["action_type"]
        key = f"{deal_id}:{run.step}:{p['idx']}"                  # deal id plus step, never random
        with self.conn.transaction():
            self._check_citations(deal, payload)
            request = self._policy_request(atype, deal, pack, payload)
            payload = self._threaded(deal_id, atype, payload)
            proposal = propose_action(
                self.conn, self.clock, self.policy_config(pack), key=key, request=request,
                deal_id=deal_id, contact_id=UUID(contact_id) if contact_id else None,
                payload=payload)
            action = self.conn.cursor(row_factory=dict_row).execute(
                "SELECT id, status, hold_reason, veto_until FROM actions WHERE id = %s",
                (proposal.action_id,)).fetchone()
            approved = action["status"] == "approved"
            if payload.get("template") == "meeting_offer" and payload.get("slots"):
                self._merge_fields(deal_id, {"offered_slots": payload["slots"]})
                self._audit_slots(deal_id, payload)
            if atype == "send_io" and approved:
                self._merge_fields(deal_id, {"io_approved": True})
            if atype in INTERNAL and approved:
                self._effect(atype, deal, pack, payload, run)
            elif atype in SENDABLE and action["status"] in {"approved", "pending"}:
                self._queue_send(action, key)
            if payload.get("fallback") and proposal.created:
                write_audit(self.conn, "orchestrator", "draft.fell_back_to_template", deal_id, {
                    "action_id": str(action["id"]), "template": payload.get("template"),
                    "checks_failed": payload["fallback"]["checks_failed"],
                    "attempts": payload["fallback"]["attempts"],
                    "reason": "the model-written text failed a check; the pack's own wording was used"})
            if payload.get("lesson_ids") and proposal.created:
                write_audit(self.conn, "orchestrator", "lesson.cited", deal_id, {
                    "action_id": str(action["id"]), "action_type": atype,
                    "lesson_ids": payload["lesson_ids"], "angle": payload.get("angle"),
                    "run": run.step})
            if proposal.created:
                self._notify(action["id"])
            outcome = (proposal.decision.outcome.value if proposal.decision
                       else f"existing:{action['status']}")
            rejected = action["status"] == "cancelled"
            self.conn.execute(
                "UPDATE agent_proposals SET status = %s, action_id = %s, outcome = %s "
                "WHERE run_id = %s AND idx = %s",
                ("rejected" if rejected else "applied", action["id"], outcome, run.run_id, p["idx"]))
        return {"idx": p["idx"], "action_type": atype, "action_id": action["id"],
                "status": action["status"], "outcome": outcome}

    def _threaded(self, deal_id: UUID, atype: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Give a follow-up its place in the thread: In-Reply-To the message it answers (or our
        last one), and References listing the thread so far, oldest first."""
        if atype in {"first_touch", "follow_up"}:
            payload = {**payload, "deal_tag": str(deal_id)[:8]}      # goes in the Reply-To plus address
        if atype != "follow_up":
            return payload
        # the id the recipient actually sees (Gmail may rewrite ours) is the one to thread under
        ids = [r[0] for r in self.conn.execute(
            "SELECT COALESCE(real_message_id, message_id) FROM messages WHERE deal_id = %s AND "
            "message_id IS NOT NULL ORDER BY id", (deal_id,))]
        if not ids:
            return payload
        reply_to = payload.get("in_reply_to") or ids[-1]
        return {**payload, "in_reply_to": reply_to,
                "references": [i for i in ids if i != reply_to][-9:] + [reply_to]}

    def _check_citations(self, deal: Mapping[str, Any], payload: Mapping[str, Any]) -> None:
        """A lesson may be cited only if it is live and belongs to this segment or to everyone."""
        ids = [int(i) for i in payload.get("lesson_ids", [])]
        if not ids:
            return
        valid = {r[0] for r in self.conn.execute(
            "SELECT id FROM lessons WHERE id = ANY(%s) AND retired_at IS NULL AND "
            "(segment_id IS NULL OR segment_id = %s)", (ids, deal["segment_id"])).fetchall()}
        if set(ids) - valid:
            raise ProposalRejected("cites lesson(s) that are retired, unknown or from another "
                                   "segment: " + ", ".join(str(i) for i in sorted(set(ids) - valid)))

    def _policy_request(self, atype: str, deal: Mapping[str, Any], pack: Pack,
                        payload: Mapping[str, Any]) -> ActionRequest:
        tier = Tier(deal["tier"]) if deal["tier"] else None
        fields = deal["fields"] or {}
        if atype in {"first_touch", "follow_up"}:
            return ActionRequest(atype, tier=tier, body=str(payload.get("body", "")),
                                 claim_check_passed=self._recheck_claims(deal, pack, payload))
        if atype == "send_io":
            check = validate_io(pack, fields, today=self.clock.now().date(),
                                signer_allowed=self._signer_allowed())
            if not check.ok and not (check.hard_block and len(check.problems) == 1):
                # recomputed from the database; the agent's word is not enough
                raise ProposalRejected("the IO does not validate: " + "; ".join(check.problems))
            # a discount over the maximum is proposed on purpose: the policy engine hard-blocks it
            return ActionRequest(atype, io_validated=True,
                                 discount_pct=float(fields.get("discount_pct", 0) or 0))
        if atype == "book_meeting":
            if "meeting_start" in fields:
                raise ProposalRejected("a meeting is already booked for this deal")
            if payload.get("start") not in (fields.get("offered_slots") or []):
                raise ProposalRejected("the time is not one we offered")
            return ActionRequest(atype)
        if atype == "attach_case_study":
            return ActionRequest(atype, from_library=any(
                c.id == payload.get("case_study_id") for c in pack.case_studies))
        return ActionRequest(atype)

    def _signer_allowed(self):
        """The recipient allowlist, as a test of one address (demo mode only)."""
        if not self.demo_mode:
            return None
        return lambda email: recipient_allowed(email, self.allowed_recipients, self.plus_bases)

    def _recheck_claims(self, deal: Mapping[str, Any], pack: Pack, payload: Mapping[str, Any]) -> bool:
        """The orchestrator re-runs the code checks on the draft against the DATABASE's facts."""
        template = pack.messaging.templates.get(str(payload.get("template", "")))
        if template is None:
            return False                      # an unknown template cannot be checked, so it fails
        study = next((c for c in pack.case_studies if c.id == payload.get("case_study_id")), None)
        extra = list(payload.get("approved_extra", []))
        if payload.get("template") == "case_study_reply":
            # the subject echoes our own earlier subject; never trust text the agent supplies here
            extra = [self._last_outbound_subject(deal["id"]) or "your question"]
        if payload.get("template") == "meeting_confirmation":
            start = (payload.get("invite") or {}).get("start")
            if start not in ((deal["fields"] or {}).get("offered_slots") or []):
                return False                       # a time we never offered cannot be confirmed
            # the time exactly as the prospect reads it, derived here from the database's own slot and zone
            tz = resolve_tz(self.conn.execute("SELECT timezone FROM contacts WHERE id = %s",
                                              (deal["contact_id"],)).fetchone()[0])
            extra = [slot_label(datetime.fromisoformat(start), tz)]
        approved, names = check_inputs(pack, template, self.identity, deal["company_name"] or "",
                                       deal["contact_name"], study, extra)
        draft = Draft(payload["subject"], payload["body"], tuple(
            Claim(c["text"], tuple(c["evidence"])) for c in payload.get("claims", [])))
        issues = deterministic_check(draft, evidence_from_fields(deal["fields"] or {}), approved, names)
        return not issues

    def _notify(self, action_id: UUID) -> None:
        """Ask for the action's Slack message to be posted or brought up to date (a job, so a Slack
        outage never touches the action itself). One event per (action, state), so repeats do nothing."""
        if self.notifier is None:
            return
        row = self.conn.execute("SELECT status, decision, sent_at IS NOT NULL, type FROM actions "
                                "WHERE id = %s", (action_id,)).fetchone()
        if row[3] not in POSTED_TYPES:
            return
        record_event(self.conn, f"slack:{action_id}:{row[0]}:{row[1]}:{row[2]}", "slack.sync",
                     {"action_id": str(action_id)}, self.clock)

    def _queue_send(self, action: Mapping[str, Any], key: str) -> None:
        if action["status"] == "approved":
            record_event(self.conn, f"send:{key}", "action.send", {"action_id": str(action["id"])},
                         self.clock)
        elif action["veto_until"] is not None:
            record_event(self.conn, f"veto:{key}", "action.veto_expired",
                         {"action_id": str(action["id"])}, self.clock, run_at=action["veto_until"])

    # --- internal effects -------------------------------------------------------------------------
    def _effect(self, atype: str, deal: Mapping[str, Any], pack: Pack, payload: dict[str, Any],
                run: RunResult) -> None:
        deal_id = deal["id"]
        if atype == "enrich":
            allowed = set(pack.scoring.lead_fields) | set(pack.stages.qualification_fields) \
                | EXTRA_FIELDS
            props = dict(payload["properties"])
            bad = [k for k in props if k not in allowed and not k.startswith(FIELD_PREFIXES)]
            if bad:
                raise ProposalRejected("field(s) not allowed for enrichment: " + ", ".join(bad))
            self._merge_fields(deal_id, props)
        elif atype == "score":
            facts = lead_facts(deal["fields"] or {}, pack)
            result = score_lead(facts, pack.scoring)
            if (result.total, result.tier.value) != (payload["total"], payload["tier"]):
                raise ProposalRejected("the proposed score differs from the pack's rules")
            self.conn.execute(
                "UPDATE deals SET tier = %s, fit_score = %s, intent_score = %s, engagement_score = %s, "
                "total_score = %s, score_reason = %s, updated_at = now() WHERE id = %s",
                (result.tier.value, result.fit, result.intent, result.engagement, result.total,
                 result.summary, deal_id))
        elif atype == "classify_reply":
            if payload.get("message_id"):
                self.conn.execute("UPDATE messages SET classification = %s WHERE message_id = %s",
                                  (payload["label"], payload["message_id"]))
            write_audit(self.conn, "orchestrator", "reply.classified", deal_id,
                        {"label": payload["label"], "confidence": payload["confidence"]})
            if payload.get("corrected_from"):       # code overruled the model's label: on the record
                write_audit(self.conn, "orchestrator", "reply.label_corrected", deal_id, {
                    "from": payload["corrected_from"], "to": payload["label"],
                    "message_id": payload.get("message_id"),
                    "reason": "no meeting times had been offered, so no time could have been accepted"})
            self._learn_from_reply(deal, payload["label"])
        elif atype in {"suppress_contact", "cancel_scheduled_sends"}:
            self._opt_out(deal, atype, payload)
        elif atype == "attach_case_study":
            self._merge_fields(deal_id, {"proposed_case_study": payload["case_study_id"]})
        elif atype == "generate_io":
            check = validate_io(pack, deal["fields"] or {}, today=self.clock.now().date())
            self.conn.execute(
                "INSERT INTO documents (deal_id, kind, version, body, validation) VALUES "
                "(%s, 'io', (SELECT coalesce(max(version), 0) + 1 FROM documents WHERE deal_id = %s "
                "AND kind = 'io'), %s, %s)",
                (deal_id, deal_id, payload["document"], Jsonb({
                    "ok": check.ok, "problems": list(check.problems), "total": check.total,
                    "line_items": list(check.line_items), "monthly_subtotal": check.monthly_subtotal,
                    "months": check.billed_months, "term_subtotal": check.term_subtotal,
                    "package": check.package_id})))
            self._merge_fields(deal_id, {"io_validated": check.ok})      # from the rules, not the agent
        elif atype == "lesson_write":
            self._write_lesson(deal, payload)
        elif atype == "snooze_ooo":
            self._snooze(deal, payload)
        else:                                                   # reminder
            write_audit(self.conn, "orchestrator", f"agent.{atype}", deal_id, dict(payload))

    def _learn_from_reply(self, deal: Mapping[str, Any], label: str) -> None:
        deal_id = deal["id"]
        if label in {"price_objection", "question"}:
            self.conn.execute(
                "UPDATE deals SET fields = jsonb_set(fields, '{open_objections}', "
                "coalesce(fields->'open_objections', '[]'::jsonb) || to_jsonb(%s::text)) "
                "WHERE id = %s AND NOT coalesce(fields->'open_objections', '[]'::jsonb) ? %s",
                (label, deal_id, label))
        outcome = {"interested": "reply_positive", "question": "reply_positive",
                   "price_objection": "reply_positive", "referral": "reply_positive",
                   "not_interested": "reply_negative", "unsubscribe": "reply_negative"}.get(label)
        if outcome:                     # an out-of-office is not a person's answer: no outcome
            lesson_loop.record_outcome(self.conn, deal_id, outcome, self.clock)

    def _snooze(self, deal: Mapping[str, Any], payload: Mapping[str, Any]) -> None:
        """Pause the lead until the stated return date, bounded, and wake it up with a timer."""
        deal_id = deal["id"]
        fields = deal["fields"] or {}
        count = int(fields.get("snooze_count", 0))
        if count >= MAX_SNOOZES:
            raise ProposalRejected(f"{count} out-of-office replies already; a person should look")
        now = self.clock.now()
        source, stated = "default", payload.get("until")
        until = now + timedelta(days=DEFAULT_SNOOZE_DAYS)
        if stated:
            until = datetime.combine(datetime.fromisoformat(stated).date(), time(9), tzinfo=UTC)
            source = "stated"
        bounded = min(max(until, now + timedelta(days=1)), now + timedelta(days=MAX_SNOOZE_DAYS))
        self._merge_fields(deal_id, {"snoozed_until": bounded.isoformat(), "snooze_count": count + 1})
        self.conn.execute(
            "UPDATE actions SET run_at = greatest(run_at, %s) WHERE deal_id = %s AND type = "
            "'follow_up' AND status IN ('approved', 'pending')", (bounded, deal_id))
        record_event(self.conn, f"snooze:{deal_id}:{payload.get('message_id')}", "lead.snooze_ended",
                     {"deal_id": str(deal_id), "until": bounded.isoformat()}, self.clock,
                     run_at=bounded)
        write_audit(self.conn, "orchestrator", "reply.snoozed", deal_id, {
            "until": bounded.isoformat(), "source": source, "bounded": bounded != until,
            "snooze_count": count + 1})

    def resume_after_snooze(self, deal_id: UUID) -> PipelineResult:
        """The snooze ended: follow up, unless the lead has since ended or opted out."""
        deal = self._deal(deal_id)
        suppressed = self.conn.execute("SELECT suppressed FROM contacts WHERE id = %s",
                                       (deal["contact_id"],)).fetchone()
        if DealStage(deal["stage"]) in {DealStage.DO_NOT_CONTACT, DealStage.CLOSED_LOST,
                                       DealStage.CLOSED_WON, DealStage.HANDOFF} \
                or (suppressed and suppressed[0]):
            write_audit(self.conn, "orchestrator", "lead.snooze_skipped", deal_id,
                        {"stage": deal["stage"]})
            return PipelineResult("completed", deal["stage"], self.runner.name, "lead has ended")
        self.conn.execute("UPDATE deals SET fields = fields - 'snoozed_until', updated_at = now() "
                          "WHERE id = %s", (deal_id,))
        step = f"outreach.snooze.{int((deal['fields'] or {}).get('snooze_count', 1))}"
        try:
            run = self.run_agent(deal_id, "outreach", step, input_={"kind": "follow_up"})
            if run.status != "completed":
                write_audit(self.conn, "orchestrator", "pipeline.needs_human", deal_id,
                            {"runner": self.runner.name, "reason": run.human_reason})
                return PipelineResult("needs_human", deal["stage"], self.runner.name,
                                      run.human_reason, runs=(("outreach", run.status),))
            applied = self.apply_run(run, deal_id)
        except tuple(cls for cls, _ in HUMAN_CAUSES) as exc:
            return PipelineResult("needs_human", deal["stage"], self.runner.name,
                                  f"{cause_of(exc)}: {exc}")
        return PipelineResult("action_proposed", deal["stage"], self.runner.name,
                              action_id=applied[0]["action_id"] if applied else None,
                              runs=(("outreach", "completed"),))

    def _merge_fields(self, deal_id: UUID, props: Mapping[str, Any]) -> None:
        self.conn.execute("UPDATE deals SET fields = fields || %s, updated_at = now() WHERE id = %s",
                          (Jsonb(dict(props)), deal_id))

    def _opt_out(self, deal: Mapping[str, Any], atype: str, payload: Mapping[str, Any]) -> None:
        contact = deal["contact_id"]
        if contact is None:
            return
        if atype == "suppress_contact":
            self.conn.execute(
                "UPDATE contacts SET suppressed = true, suppressed_reason = %s, "
                "suppressed_at = coalesce(suppressed_at, now()) WHERE id = %s",
                (payload.get("reason", "opt-out"), contact))
        cancelled = self.conn.execute(
            "UPDATE actions SET status = 'cancelled', decision = 'rejected', "
            "decision_by = 'policy:opt_out', decided_at = now(), hold_reason = 'opt_out' "
            "WHERE contact_id = %s AND status IN ('pending', 'awaiting_approval', 'approved') "
            "AND type IN ('first_touch', 'follow_up', 'send_io')", (contact,)).rowcount
        write_audit(self.conn, "orchestrator", f"agent.{atype}", deal["id"],
                    {"contact_id": str(contact), "sends_cancelled": cancelled})

    def _write_lesson(self, deal: Mapping[str, Any], payload: Mapping[str, Any]) -> None:
        evidence = UntrustedText.wrap("transcript", str(payload.get("origin", "lesson")),
                                      str(payload["evidence"]))
        if evidence.flagged:
            raise ProposalRejected("the lesson's evidence quote looks like an instruction")
        same = self.conn.execute("SELECT 1 FROM lessons WHERE segment_id = %s AND lower(text) = lower(%s)",
                                 (deal["segment_id"], payload["text"])).fetchone()
        if same is None:
            self.conn.execute("INSERT INTO lessons (segment_id, text, evidence, tags, source) "
                              "VALUES (%s, %s, %s, %s, %s)",
                              (deal["segment_id"], payload["text"], payload["evidence"],
                               list(payload.get("tags", [])), payload.get("source", "call")))

    # --- stage moves: only here -------------------------------------------------------------------
    def advance(self, deal_id: UUID, to_stage: DealStage, reason: str) -> bool:
        deal = self._deal(deal_id)
        current = DealStage(deal["stage"])
        if current == to_stage or not can_transition(current, to_stage):
            return False
        transition_deal(self.conn, deal_id, to_stage, actor="orchestrator", reason=reason,
                        exit_criteria=self._pack(deal).stages.exit_criteria)
        self.sync_to_crm(deal_id)
        return True

    # --- the slice: lead.created -> enrichment -> scoring -> outreach proposal --------------------
    def handle_lead_created(self, deal_id: UUID) -> PipelineResult:
        runs: list[tuple[str, str]] = []

        def stop(status: str, reason: str | None, **kw: Any) -> PipelineResult:
            self.sync_to_crm(deal_id)
            stage = self._deal(deal_id)["stage"]
            if status == "needs_human":
                write_audit(self.conn, "orchestrator", "pipeline.needs_human", deal_id,
                            {"runner": self.runner.name, "stage": stage, "reason": reason})
            return PipelineResult(status, stage, self.runner.name, reason, runs=tuple(runs), **kw)

        try:
            for agent, step, stage_in, stage_out in (
                    ("enrichment", "enrich", DealStage.NEW, DealStage.ENRICHED),
                    ("scoring", "score", DealStage.ENRICHED, DealStage.SCORED)):
                if DealStage(self._deal(deal_id)["stage"]) is not stage_in:
                    continue
                run = self.run_agent(deal_id, agent, step)
                runs.append((agent, run.status))
                if run.status != "completed":
                    return stop("needs_human", run.human_reason)
                self.apply_run(run, deal_id)
                self.advance(deal_id, stage_out, f"{agent} finished")

            deal = self._deal(deal_id)
            if deal["tier"] == Tier.DISQUALIFIED.value:
                self.advance(deal_id, DealStage.CLOSED_LOST, "disqualified by the scoring rules")
                return stop("closed", "disqualified by the scoring rules")
            if DealStage(deal["stage"]) is not DealStage.SCORED:
                return stop("completed", None)

            run = self.run_agent(deal_id, "outreach", "outreach.1", input_={"kind": "first_touch"})
            runs.append(("outreach", run.status))
            if run.status != "completed":
                return stop("needs_human", run.human_reason)
            applied = self.apply_run(run, deal_id)
            first = applied[0] if applied else None
            return stop("action_proposed", None, action_id=first["action_id"] if first else None,
                        decision=first["outcome"] if first else None)
        except tuple(cls for cls, _ in HUMAN_CAUSES) as exc:
            return stop("needs_human", f"{cause_of(exc)}: {exc}")

    # --- a person enters terms or approves a discount (agents cannot) -----------------------------
    def set_io_terms(self, deal_id: UUID, *, items: list[dict[str, Any]], start: str, end: str,
                     discount_pct: float, actor: str, detail: Mapping[str, Any] | None = None,
                     package: str = "") -> None:
        """Set the IO's terms. `actor` 'io-planner' is the automatic proposal; anyone else is a person's
        override. Both are audited, and a person's override is never replaced by a proposal."""
        source = "auto" if actor == "io-planner" else "override"
        self._merge_fields(deal_id, {"io_items": items, "io_start_date": start, "io_end_date": end,
                                     "discount_pct": discount_pct, "io_terms_source": source,
                                     "io_package": package})
        write_audit(self.conn, actor, "deal.io_terms_set", deal_id,
                    {"items": items, "start": start, "end": end, "discount_pct": discount_pct,
                     "source": source, **(dict(detail) if detail else {})})

    def _propose_terms(self, deal_id: UUID) -> PipelineResult | None:
        """No terms yet: work them out from the call and the pack's SAMPLE rate card. Returns a result only
        when a person is needed (a term outside the pack's limits); None when terms are now in place."""
        deal = self._deal(deal_id)
        fields = clean_fields(deal["fields"] or {})
        if fields.get("io_items"):
            return None                                     # a person's terms (or an earlier proposal) stand
        proposal = propose_io_terms(self._pack(deal), fields, self.clock.now().date())
        if not proposal.ok:
            write_audit(self.conn, "io-planner", "io.terms_need_a_person", deal_id, {
                "reasons": list(proposal.needs_a_person), "package": proposal.package_id,
                "monthly_budget_usd": proposal.monthly_budget_usd})
            why = "; ".join(proposal.needs_a_person)
            return PipelineResult("needs_human", deal["stage"], self.runner.name,
                                  f"IO terms fall outside the pack's limits: {why}")
        self.set_io_terms(deal_id, items=list(proposal.items), start=proposal.start, end=proposal.end,
                          discount_pct=proposal.discount_pct, actor="io-planner",
                          package=proposal.package_id, detail={
                              "package": proposal.package_id, "notes": list(proposal.notes),
                              "monthly_budget_usd": proposal.monthly_budget_usd,
                              "budget_quote": fields.get("quote:budget"),
                              "timeline_quote": fields.get("quote:timeline"), "deadline": proposal.deadline})
        return None

    # --- the qualification gate -------------------------------------------------------------------
    def run_qualification_gate(self, deal_id: UUID) -> PipelineResult:
        """Check the pack's criteria for 'qualified'. The agent only suggests; the deal moves only
        through transition_deal, which checks the same criteria again."""
        deal = self._deal(deal_id)
        fields = deal["fields"] or {}
        step = "qualify." + canonical_hash({k: fields.get(k) for k in QUALIFICATION_KEYS})
        try:
            run = self.run_agent(deal_id, "qualification", step)
            if run.status != "completed":
                return PipelineResult("needs_human", deal["stage"], self.runner.name, run.human_reason)
            self.apply_run(run, deal_id)
            if run.output.get("stage_suggestion") != "qualified":
                return PipelineResult("held", deal["stage"], self.runner.name, run.output.get("notes"),
                                      runs=(("qualification", "completed"),))
            if DealStage(deal["stage"]) is not DealStage.MEETING:
                return PipelineResult("held", deal["stage"], self.runner.name,
                                      f"criteria met, but the deal is in {deal['stage']}, not meeting",
                                      runs=(("qualification", "completed"),))
            self.advance(deal_id, DealStage.QUALIFIED, "exit criteria met")
        except tuple(cls for cls, _ in HUMAN_CAUSES) as exc:
            return PipelineResult("needs_human", self._deal(deal_id)["stage"], self.runner.name,
                                  f"{cause_of(exc)}: {exc}")
        return PipelineResult("advanced", self._deal(deal_id)["stage"], self.runner.name,
                              runs=(("qualification", "completed"),))

    # --- the IO -----------------------------------------------------------------------------------
    def handle_io(self, deal_id: UUID) -> PipelineResult:
        """Build and validate the IO, then let the policy engine decide about sending it."""
        deal = self._deal(deal_id)
        if DealStage(deal["stage"]) not in {DealStage.QUALIFIED, DealStage.PROPOSAL,
                                           DealStage.IO_APPROVAL}:
            return PipelineResult("held", deal["stage"], self.runner.name,
                                  f"an IO needs a qualified deal, this one is in {deal['stage']}")
        needs_person = self._propose_terms(deal_id)
        if needs_person is not None:
            return needs_person
        fields = self._deal(deal_id)["fields"] or {}
        step = "io." + canonical_hash({k: fields.get(k) for k in IO_KEYS})
        try:
            self.advance(deal_id, DealStage.PROPOSAL, "building the IO")
            run = self.run_agent(deal_id, "contract_io", step)
            if run.status != "completed":
                return PipelineResult("needs_human", self._deal(deal_id)["stage"], self.runner.name,
                                      run.human_reason, runs=(("contract_io", run.status),))
            applied = self.apply_run(run, deal_id)
            if (self._deal(deal_id)["fields"] or {}).get("io_validated") is True:
                self.advance(deal_id, DealStage.IO_APPROVAL, "the IO validates")
            runs = (("contract_io", "completed"),)
            handoff = run.output.get("handoff")
            send = next((a for a in applied if a["action_type"] == "send_io"), None)
            if handoff or (send and send["status"] != "approved"):
                reason = handoff or self._block_reason(send["action_id"])
                write_audit(self.conn, "orchestrator", "pipeline.needs_human", deal_id,
                            {"runner": self.runner.name, "reason": reason})
                return PipelineResult("needs_human", self._deal(deal_id)["stage"], self.runner.name,
                                      reason, action_id=send["action_id"] if send else None, runs=runs)
            return PipelineResult("action_proposed", self._deal(deal_id)["stage"], self.runner.name,
                                  action_id=send["action_id"] if send else None,
                                  decision=send["outcome"] if send else None, runs=runs)
        except tuple(cls for cls, _ in HUMAN_CAUSES) as exc:
            return PipelineResult("needs_human", self._deal(deal_id)["stage"], self.runner.name,
                                  f"{cause_of(exc)}: {exc}")

    def _block_reason(self, action_id: UUID) -> str:
        row = self.conn.execute("SELECT status, hold_reason FROM actions WHERE id = %s",
                                (action_id,)).fetchone()
        return f"blocked, a person must decide: {row[1] or row[0]}"

    # --- signature: verified, idempotent, then closed won and the handoff -------------------------
    def handle_signature_webhook(self, payload: Mapping[str, Any]) -> str:
        """Receive a signature webhook. Returns 'queued', 'duplicate' or 'rejected'."""
        if self.esign is None:
            raise LookupError("no e-signature provider is configured")
        try:
            event = self.esign.parse_event(payload)
        except WebhookRejected as exc:
            write_audit(self.conn, "webhook", "webhook.rejected", None, {"why": str(exc)})
            return "rejected"
        created = record_event(self.conn, f"signature:{event.event_id}", "signature.event",
                               {"envelope_id": event.envelope_id, "kind": event.kind}, self.clock)
        return "queued" if created is not None else "duplicate"

    def note_signature_event(self, envelope_id: str, kind: str, event_type: str) -> None:
        """A progress event from the provider (sent, viewed, signed by one signer): audited, once."""
        row = self.conn.execute("SELECT deal_id FROM actions WHERE type = 'send_io' AND provider_id = %s",
                                (envelope_id,)).fetchone()
        write_audit(self.conn, "webhook", f"signature.{kind}", row[0] if row else None,
                    {"envelope_id": envelope_id, "event_type": event_type})

    def complete_signature(self, envelope_id: str) -> PipelineResult:
        """Close the deal once the provider itself confirms the signature. Safe to repeat."""
        action = self.conn.cursor(row_factory=dict_row).execute(
            "SELECT deal_id FROM actions WHERE type = 'send_io' AND provider_id = %s",
            (envelope_id,)).fetchone()
        if action is None:                         # an envelope we never sent: ignore, do not guess
            write_audit(self.conn, "orchestrator", "signature.unknown_envelope", None,
                        {"envelope_id": envelope_id})
            return PipelineResult("rejected", "unknown", self.runner.name, "unknown envelope")
        deal_id = action["deal_id"]
        deal = self._deal(deal_id)
        if self.esign.status(envelope_id) != "signed":
            write_audit(self.conn, "orchestrator", "signature.not_confirmed", deal_id,
                        {"envelope_id": envelope_id})
            return PipelineResult("held", deal["stage"], self.runner.name,
                                  "the provider does not show it as signed")
        if DealStage(deal["stage"]) is not DealStage.OUT_FOR_SIGNATURE:
            return PipelineResult("completed", deal["stage"], self.runner.name, "already handled")
        self._merge_fields(deal_id, {"signature_complete": True,
                                     "signed_at": self.clock.now().isoformat()})
        self.advance(deal_id, DealStage.CLOSED_WON, "signed")
        write_audit(self.conn, "orchestrator", "deal.signed", deal_id, {"envelope_id": envelope_id})
        self._crm_note(deal_id, f"{deal_id}:signed:{envelope_id}",
                       f"Signed (TEST MODE, not legally binding): signature request {envelope_id}. "
                       "The deal is closed won.")
        return self._create_handoff(deal_id, envelope_id)

    def _create_handoff(self, deal_id: UUID, envelope_id: str) -> PipelineResult:
        deal = self._deal(deal_id)
        pack = self._pack(deal)
        fields = deal["fields"] or {}
        document = self.conn.cursor(row_factory=dict_row).execute(
            "SELECT validation FROM documents WHERE deal_id = %s AND kind = 'io' ORDER BY version "
            "DESC LIMIT 1", (deal_id,)).fetchone()
        lesson_ids = [r[0] for r in self.conn.execute(
            "SELECT DISTINCT lesson_id FROM lesson_uses WHERE deal_id = %s ORDER BY 1", (deal_id,))]
        text = build_handoff(
            company=deal["company_name"] or "", contact=deal["contact_name"], tier=deal["tier"],
            total=deal["total_score"], fields=clean_fields(fields), terms=document["validation"],
            rate_card_label=pack.rate_card.label, envelope_id=envelope_id, lesson_ids=lesson_ids)
        self._merge_fields(deal_id, {"handoff_owner": self.customer_success_owner})
        proposal = propose_action(
            self.conn, self.clock, self.policy_config(pack), key=f"{deal_id}:handoff",
            request=ActionRequest("handoff"), deal_id=deal_id,
            payload={"channel": self.handoff_channel, "text": text, "envelope_id": envelope_id})
        action = self.conn.execute("SELECT id, status, hold_reason, veto_until FROM actions "
                                   "WHERE id = %s", (proposal.action_id,)).fetchone()
        if action[1] == "approved":
            record_event(self.conn, f"send:{deal_id}:handoff", "action.send",
                         {"action_id": str(action[0])}, self.clock)
        return PipelineResult("completed", self._deal(deal_id)["stage"], self.runner.name,
                              action_id=action[0], decision=action[1])

    # --- a call: transcript -> deal fields -> lessons ---------------------------------------------
    def handle_transcript(self, deal_id: UUID, transcript_id: str,
                          transcript: Any = None) -> PipelineResult:
        """Turn a finished call into deal fields (each with its quote) and lessons (each with its
        evidence). A transcript with an instruction in it is quarantined and teaches nothing."""
        if transcript is None:
            if self.transcripts is None:
                raise LookupError("no transcript source is configured")
            transcript = self.transcripts.get(transcript_id)
        if transcript is None:
            raise LookupError(f"no transcript {transcript_id}")
        text = UntrustedText.wrap("transcript", transcript.id, transcript.text)
        runs: list[tuple[str, str]] = []
        lessons_before = self.conn.execute("SELECT coalesce(max(id), 0) FROM lessons").fetchone()[0]

        def done(status: str, reason: str | None = None) -> PipelineResult:
            if status == "completed":
                new = [r[0] for r in self.conn.execute(
                    "SELECT id FROM lessons WHERE id > %s ORDER BY id", (lessons_before,))]
                self._crm_note(deal_id, f"{deal_id}:callsummary:{transcript_id}", call_summary(
                    transcript.title, clean_fields(self._deal(deal_id)["fields"] or {}), new))
                self._slack_call(deal_id, transcript_id, transcript.title)
            self.sync_to_crm(deal_id)
            if status == "needs_human":
                write_audit(self.conn, "orchestrator", "pipeline.needs_human", deal_id,
                            {"runner": self.runner.name, "reason": reason, "transcript": transcript_id,
                             "quarantined": text.flagged})
            return PipelineResult(status, self._deal(deal_id)["stage"], self.runner.name, reason,
                                  runs=tuple(runs))

        try:
            run = self.run_agent(deal_id, "call_intel", f"call:{transcript_id}",
                                 untrusted={"transcript": text})
            runs.append(("call_intel", run.status))
            if run.status != "completed":
                return done("needs_human", run.human_reason)
            self.apply_run(run, deal_id)
            payload = run.proposals[0]["payload"] if run.proposals else {}
            for field, why in (payload.get("rejected") or {}).items():
                write_audit(self.conn, "orchestrator", "call.field_dropped", deal_id,
                            {"transcript": transcript_id, "field": field, "reason": why})
            quotes = payload.get("quotes", {})
            for name in ("objections", "needs"):
                if name in quotes:
                    self._learn(deal_id, f"know:{transcript_id}:{name}", quotes[name],
                                f"{transcript_id}#{name}", "call", runs)
            if run.output.get("handoff"):            # the fields stand, but a person must say who signs
                return done("needs_human", run.output["handoff"])
            return done("completed")
        except tuple(cls for cls, _ in HUMAN_CAUSES) as exc:
            return done("needs_human", f"{cause_of(exc)}: {exc}")

    # --- finished calls from the transcript service: matched with evidence, never guessed ----------
    def _open_deals(self) -> list[DealFacts]:
        """The deals a finished call can belong to: those whose meeting is booked and not yet followed by a
        call (stage `meeting`). A call cannot precede its meeting, so a deal still in outreach is never a
        candidate: its call must not be taken (or held) before the prospect has even answered."""
        rows = self.conn.cursor(row_factory=dict_row).execute(
            "SELECT d.id, co.name AS company, c.email, d.fields->>'meeting_start' AS meeting_start "
            "FROM deals d JOIN contacts c ON c.id = d.contact_id "
            "LEFT JOIN companies co ON co.id = d.company_id "
            "WHERE d.stage = 'meeting'").fetchall()
        facts = []
        for r in rows:
            try:
                start = datetime.fromisoformat(r["meeting_start"]) if r["meeting_start"] else None
            except ValueError:
                start = None
            facts.append(DealFacts(r["id"], r["company"] or "", r["email"], start))
        return facts

    def ingest_transcript(self, transcript_id: str) -> str:
        """Match one finished transcript to a deal and process it. Returns 'processed', 'seen', 'held',
        'waiting' (no deal has a booked meeting yet) or 'missing'. Safe to call any number of times."""
        if self.transcripts is None:
            return "missing"
        if self.conn.execute("SELECT 1 FROM transcripts WHERE external_id = %s UNION ALL "
                             "SELECT 1 FROM held_calls WHERE external_id = %s",
                             (transcript_id, transcript_id)).fetchone():
            return "seen"
        transcript = self.transcripts.get(transcript_id)
        if transcript is None:
            return "missing"
        candidates = self._open_deals()
        if not candidates:
            return "waiting"          # no meeting is booked yet: leave the recording alone and hold nothing
        match = match_call(transcript, candidates)
        if match.deal_id is None:               # ambiguous, or no evidence at all
            with self.conn.transaction():
                self.conn.execute(
                    "INSERT INTO held_calls (external_id, title, started_at, reason, candidate_deal_ids) "
                    "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (external_id) DO NOTHING",
                    (transcript_id, transcript.title, transcript.date, match.kind, list(match.candidates)))
                write_audit(self.conn, "orchestrator", "call.unmatched", None, {
                    "transcript": transcript_id, "title": transcript.title, "reason": match.kind,
                    "candidates": [str(c) for c in match.candidates]})
            return "held"
        return self._process_call(match.deal_id, transcript, list(match.by))

    def _process_call(self, deal_id: UUID, transcript: Any, by: list[str],
                      undo: Callable[[], None] | None = None) -> str:
        """`undo` is set when the transcript row already exists (a reassignment): a failure puts it back."""
        transcript_id = transcript.id
        if undo is None:
            first = self.conn.execute(
                "INSERT INTO transcripts (deal_id, external_id, body) VALUES (%s, %s, %s) "
                "ON CONFLICT (external_id) DO NOTHING RETURNING id", (deal_id, transcript_id,
                                                                      transcript.text)).fetchone()
            if first is None:
                return "seen"
            undo = lambda: self.conn.execute(      # noqa: E731
                "DELETE FROM transcripts WHERE id = %s", (first[0],))
        write_audit(self.conn, "orchestrator", "call.matched", deal_id, {
            "transcript": transcript_id, "by": by})
        try:
            result = self.handle_transcript(deal_id, transcript_id, transcript)
            if result.status == "completed" and \
                    DealStage(self._deal(deal_id)["stage"]) is DealStage.MEETING:
                gate = self.run_qualification_gate(deal_id)            # criteria met: meeting -> qualified
                write_audit(self.conn, "orchestrator", "call.qualification", deal_id, {
                    "transcript": transcript_id, "result": gate.status, "note": gate.reason,
                    "stage": gate.stage})
                if DealStage(self._deal(deal_id)["stage"]) is DealStage.QUALIFIED:
                    io = self.handle_io(deal_id)               # qualified: the IO follows by itself
                    write_audit(self.conn, "orchestrator", "call.io", deal_id, {
                        "transcript": transcript_id, "result": io.status, "note": io.reason})
        except Exception:
            undo()                                                             # let the poll retry
            raise
        return "processed"

    def reassign_call(self, transcript_id: str, deal_id: UUID, actor: str) -> str:
        """Move a call that was handled for a CLOSED deal onto an open one and process it there, once.
        Audited. A call on a live deal is never moved, and the old deal keeps its own record."""
        row = self.conn.execute(
            "SELECT t.id, t.deal_id, d.stage FROM transcripts t JOIN deals d ON d.id = t.deal_id "
            "WHERE t.external_id = %s", (transcript_id,)).fetchone()
        if row is None:
            raise LookupError(f"no handled call {transcript_id}")
        old_deal, old_stage = row[1], row[2]
        if old_deal == deal_id:
            return "seen"
        if old_stage not in CLOSED_STAGES:
            raise LookupError("that call belongs to a deal that is still open: not moved")
        if DealStage(self._deal(deal_id)["stage"]).value in CLOSED_STAGES:
            raise LookupError("the target deal is closed")
        if self.transcripts is None or (transcript := self.transcripts.get(transcript_id)) is None:
            raise LookupError(f"no transcript {transcript_id}")
        self.conn.execute("UPDATE transcripts SET deal_id = %s, extracted = NULL WHERE id = %s",
                          (deal_id, row[0]))
        write_audit(self.conn, actor, "call.reassigned", deal_id, {
            "transcript": transcript_id, "from_deal": str(old_deal), "from_stage": old_stage})
        return self._process_call(deal_id, transcript, ["operator"], undo=lambda: self.conn.execute(
            "UPDATE transcripts SET deal_id = %s WHERE id = %s", (old_deal, row[0])))

    def assign_held_call(self, transcript_id: str, deal_id: UUID, actor: str) -> str:
        """A person decides which deal a held call belongs to. Audited; the call is then processed
        exactly as a matched one (and only once)."""
        held = self.conn.execute("SELECT 1 FROM held_calls WHERE external_id = %s AND resolved_at IS NULL",
                                 (transcript_id,)).fetchone()
        if held is None:
            raise LookupError(f"no held call {transcript_id}")
        if self.transcripts is None or (transcript := self.transcripts.get(transcript_id)) is None:
            raise LookupError(f"no transcript {transcript_id}")
        self._deal(deal_id)                                                   # must exist
        with self.conn.transaction():
            self.conn.execute("UPDATE held_calls SET resolved_at = %s WHERE external_id = %s",
                              (self.clock.now(), transcript_id))
            write_audit(self.conn, actor, "call.assigned", deal_id, {"transcript": transcript_id})
        return self._process_call(deal_id, transcript, ["operator"])

    def reset_call_match(self, transcript_id: str, actor: str) -> str:
        """Forget that a recording was handled or held, so the next poll can match it afresh. Only for a
        recording whose deal is closed, or one that is held; never a live deal's call. Audited."""
        row = self.conn.execute(
            "SELECT t.id, t.deal_id, d.stage FROM transcripts t JOIN deals d ON d.id = t.deal_id "
            "WHERE t.external_id = %s", (transcript_id,)).fetchone()
        held = self.conn.execute("SELECT 1 FROM held_calls WHERE external_id = %s",
                                 (transcript_id,)).fetchone()
        if row is None and held is None:
            raise LookupError(f"nothing is recorded for transcript {transcript_id}")
        if row is not None and row[2] not in CLOSED_STAGES:
            raise LookupError("that call belongs to a deal that is still open: not forgotten")
        with self.conn.transaction():
            if row is not None:
                self.conn.execute("DELETE FROM transcripts WHERE id = %s", (row[0],))
            self.conn.execute("DELETE FROM held_calls WHERE external_id = %s", (transcript_id,))
            write_audit(self.conn, actor, "call.match_state_reset", row[1] if row else None, {
                "transcript": transcript_id, "was": "handled" if row else "held",
                "from_stage": row[2] if row else None})
        return "reset"

    def poll_calls(self) -> list[tuple[str, str]]:
        """Look for finished transcripts we have not handled. Idempotent per transcript id."""
        if self.transcripts is None:
            return []
        return [(t.id, self.ingest_transcript(t.id)) for t in self.transcripts.list_recent()]

    def _slack_call(self, deal_id: UUID, transcript_id: str, title: str) -> None:
        if self.notifier is not None:
            record_event(self.conn, f"slack:call:{transcript_id}", "slack.call",
                         {"deal_id": str(deal_id), "transcript_id": transcript_id, "title": title},
                         self.clock)

    def _learn(self, deal_id: UUID, step: str, quote: str, origin: str, source: str,
               runs: list[tuple[str, str]]) -> None:
        candidate = UntrustedText.wrap("transcript", origin, quote)
        run = self.run_agent(deal_id, "knowledge", step, input_={"source": source},
                             untrusted={"candidate": candidate})
        runs.append(("knowledge", run.status))
        if run.status == "completed":
            self.apply_run(run, deal_id)

    def record_meeting_booked(self, deal_id: UUID, start: str | None = None
                              ) -> list[lesson_loop.LessonChange]:
        """A meeting is booked. Everything here happens once, however often it is called."""
        first = self.conn.execute(
            "UPDATE deals SET fields = fields || %s, updated_at = now() WHERE id = %s AND NOT "
            "(fields ? 'meeting_start') RETURNING id", (Jsonb({"meeting_start": start or "booked"}),
                                                        deal_id)).fetchone()
        changes = lesson_loop.record_outcome(self.conn, deal_id, "meeting", self.clock)
        self.advance(deal_id, DealStage.REPLIED, "the prospect answered")
        self.advance(deal_id, DealStage.MEETING, "a meeting was booked")
        if first:
            write_audit(self.conn, "orchestrator", "meeting.booked", deal_id, {"start": start})
        return changes

    def sweep_lessons(self) -> list[lesson_loop.LessonChange]:
        return lesson_loop.sweep_no_replies(self.conn, self.clock)

    # --- a customer reply -------------------------------------------------------------------------
    def reprocess_reply(self, deal_id: UUID, message_id: str, actor: str = "operator") -> PipelineResult:
        """Handle a stored inbound reply again under a new agent step, after a bug that mishandled it was
        fixed. Audited. Everything it sends keeps its idempotency key, so nothing goes out twice."""
        row = self.conn.execute(
            "SELECT message_id, in_reply_to, subject, body FROM messages WHERE deal_id = %s AND "
            "direction = 'inbound' AND message_id = %s", (deal_id, message_id)).fetchone()
        if row is None:
            raise ValueError(f"no stored inbound message {message_id} on this deal")
        # allowed only when the last handling left something for a person: it ended in needs_human, or its
        # reply email was blocked/cancelled afterwards. Otherwise a second run would send things again.
        left = self.conn.execute(
            "WITH last AS (SELECT id, event, at FROM audit_log WHERE deal_id = %(d)s AND event IN "
            "('pipeline.needs_human', 'reply.reprocessed') ORDER BY id DESC LIMIT 1) "
            "SELECT (SELECT event FROM last) = 'pipeline.needs_human' OR EXISTS ("
            "SELECT 1 FROM actions a, last WHERE a.deal_id = %(d)s AND a.type = 'follow_up' AND "
            "a.status = 'cancelled' AND a.created_at >= last.at)", {"d": deal_id}).fetchone()[0]
        if not left:
            raise ValueError("this deal's last reply was not left for a person: nothing to reprocess")
        runs = self.conn.execute("SELECT count(*) FROM agent_runs WHERE deal_id = %s AND agent = 'reply' "
                                 "AND step LIKE %s", (deal_id, f"reply:{message_id}%")).fetchone()[0]
        write_audit(self.conn, actor, "reply.reprocessed", deal_id, {"message_id": message_id,
                                                                       "attempt": runs + 1})
        email = SimpleNamespace(message_id=row[0], in_reply_to=row[1], subject=row[2], body=row[3],
                                is_html=False)
        return self.handle_reply(deal_id, email, attempt=runs + 1)

    def handle_reply(self, deal_id: UUID, email: Any, attempt: int = 1) -> PipelineResult:
        """Wrap, store, classify and route an inbound email. Flagged text is quarantined, never
        obeyed. A duplicate delivery of the same message does nothing."""
        deal = self._deal(deal_id)
        reply = UntrustedText.wrap("reply", email.message_id, email.body, is_html=email.is_html)
        self.conn.execute(
            "INSERT INTO messages (deal_id, contact_id, direction, message_id, in_reply_to, subject, "
            "body, quarantined) VALUES (%s, %s, 'inbound', %s, %s, %s, %s, %s) "
            "ON CONFLICT (message_id) WHERE message_id IS NOT NULL DO NOTHING",
            (deal_id, deal["contact_id"], email.message_id, email.in_reply_to, email.subject,
             email.body, reply.flagged))
        if attempt == 1:                                  # a reprocess finds it logged already
            self.log_email_to_crm(deal_id, "inbound", email.message_id, email.subject, email.body,
                                  email.received_at, email.from_addr, email.to)
        runs: list[tuple[str, str]] = []

        def done(status: str, reason: str | None = None, label: str | None = None,
                 **kw: Any) -> PipelineResult:
            self.sync_to_crm(deal_id)
            if status == "needs_human":
                write_audit(self.conn, "orchestrator", "pipeline.needs_human", deal_id,
                            {"runner": self.runner.name, "reason": reason, "label": label})
            return PipelineResult(status, self._deal(deal_id)["stage"], self.runner.name, reason,
                                  label=label, runs=tuple(runs), **kw)

        try:
            run = self.run_agent(
                deal_id, "reply", f"reply:{email.message_id}" + (f":r{attempt}" if attempt > 1 else ""),
                input_={"message_id": email.message_id,
                        "original_subject": self._last_outbound_subject(deal_id)},
                untrusted={"reply": reply})
            runs.append(("reply", run.status))
            if run.status != "completed":
                return done("needs_human", run.human_reason)
            self.apply_run(run, deal_id)
            label = next((p["payload"]["label"] for p in run.proposals
                          if p["action_type"] == "classify_reply"), None)
            self._note_reply(deal_id, email, label, next(
                (p["payload"].get("evidence_quote", "") for p in run.proposals
                 if p["action_type"] == "classify_reply"), ""))
            if run.output.get("stage_suggestion"):
                self.advance(deal_id, DealStage(run.output["stage_suggestion"]), f"reply: {label}")
            already_booked = "meeting_start" in (self._deal(deal_id)["fields"] or {})
            if label == "interested" and not already_booked:   # route: offer meeting times
                meeting = self.run_agent(
                    deal_id, "meeting", f"meeting:{email.message_id}",
                    slots=self._slots(deal_id, email.body), input_={"reply_label": label},
                    untrusted={"reply": reply})
                runs.append(("meeting", meeting.status))
                if meeting.status != "completed":
                    return done("needs_human", meeting.human_reason, label)
                self.apply_run(meeting, deal_id)
            if self.learn_from_replies and label in {"price_objection", "not_interested"}:
                self._learn(deal_id, f"know:{email.message_id}", email.body, email.message_id,
                            "reply", runs)
            if run.output.get("handoff"):
                return done("needs_human", run.output["handoff"], label)
            return done("completed", None, label)
        except tuple(cls for cls, _ in HUMAN_CAUSES) as exc:
            return done("needs_human", f"{cause_of(exc)}: {exc}")

    def _note_reply(self, deal_id: UUID, email: Any, label: str | None, quote: str) -> None:
        """A CRM note for a reply: how it was classified, the evidence, and the new text only."""
        text = (f"Reply received: {email.subject}\nClassified as: {label}"
                + (f' (evidence: "{quote}")' if quote else "") + f"\n\n{new_text(email.body)[:800]}")
        self._crm_note(deal_id, f"{deal_id}:reply-note:{email.message_id}", text)

    # --- HubSpot: its own actions, so a CRM failure never touches an email and vice versa ----------------
    def _crm_action(self, deal_id: UUID, contact_id: UUID | None, key: str, type_: str,
                    payload: dict[str, Any]) -> UUID | None:
        pack = self._pack(self._deal(deal_id))
        proposal = propose_action(self.conn, self.clock, self.policy_config(pack), key=key,
                                  request=ActionRequest(type_), deal_id=deal_id, contact_id=contact_id,
                                  payload=payload)
        if not proposal.created:
            return None
        row = self.conn.cursor(row_factory=dict_row).execute(
            "SELECT id, status, veto_until FROM actions WHERE id = %s", (proposal.action_id,)).fetchone()
        self._queue_send(row, key)
        return proposal.action_id

    def sync_to_crm(self, deal_id: UUID, refresh: str = "") -> list[UUID]:
        """Write the contact, company and deal (stage, score, tier, reason, outreach status, sequence
        day, call fields with quotes, lessons cited) to the CRM. The key includes a hash of the
        content, so nothing is queued unless something changed. `refresh` (the backfill's day) forces one
        rewrite per value, so a record gets what it lacked (an owner) although its content did not change."""
        if not self.crm_sync:
            return []
        deal = self._deal(deal_id)
        fields = clean_fields(deal["fields"] or {})
        lessons = [r[0] for r in self.conn.execute(
            "SELECT DISTINCT lesson_id FROM lesson_uses WHERE deal_id = %s ORDER BY 1", (deal_id,))]
        first = self.conn.execute(
            "SELECT min(sent_at) FROM actions WHERE deal_id = %s AND type = 'first_touch' AND "
            "status = 'sent'", (deal_id,)).fetchone()[0]
        props = build_deal_properties(deal, fields, lessons)
        props["revos_outreach_status"] = outreach_status(deal["stage"])
        if (day := sequence_day(first, self.clock.now())) is not None:
            props["revos_sequence_day"] = day
        if deal["company_name"]:
            props["dealname"] = deal["company_name"]
        names = (deal["contact_name"] or "").split(" ", 1)
        writes = [
            ("contact", {"kind": "contacts", "properties": {"firstname": names[0], "lastname":
                                                            names[1] if len(names) > 1 else ""}}),
            ("company", {"kind": "companies", "domain": deal["domain"],
                         "properties": {"name": deal["company_name"]}}) if deal["domain"] else None,
            ("deal", {"kind": "deals", "stage": deal["stage"], "properties": props,
                      "company_domain": deal["domain"]}),
        ]
        queued = []
        for write in filter(None, writes):
            name, payload = write
            digest = canonical_hash(payload) + (f":{refresh}" if refresh else "")
            action_id = self._crm_action(deal_id, deal["contact_id"], f"{deal_id}:crm:{name}:{digest}",
                                         "crm_update", payload)
            if action_id:
                queued.append(action_id)
        if deal["score_reason"]:
            # the written score on the deal's timeline, once per distinct score (the key holds a hash of it)
            text = f"Lead score {deal['total_score']}/100, tier {deal['tier']}\n{deal['score_reason']}"
            digest = canonical_hash([deal["total_score"], deal["tier"], deal["score_reason"]])
            self._crm_note(deal_id, f"{deal_id}:score-note:{digest}", text)
        return queued

    def log_email_to_crm(self, deal_id: UUID, direction: str, message_id: str, subject: str | None,
                         body: str | None, at: datetime, from_addr: str, to_addr: str) -> UUID | None:
        """Put one email on the contact's timeline in the CRM: its own outbox action, keyed by the Message-ID,
        so a retry or a repeat logs nothing twice and a CRM failure never touches the email itself."""
        if not self.crm_sync or not message_id:
            return None
        contact_id = self._deal(deal_id)["contact_id"]
        return self._crm_action(
            deal_id, contact_id, f"{deal_id}:crm:email:{message_id}", "crm_update",
            {"kind": "emails", "direction": direction, "subject": subject or "", "text": (body or "")[:6000],
             "at": at.isoformat(), "from": from_addr, "to": to_addr})

    def backfill_crm(self, deal_id: UUID) -> int:
        """Queue everything the CRM should hold about a deal that may predate this code: the deal and its
        owner, the written score and every stored email. Idempotent: what is already there queues nothing."""
        queued = len(self.sync_to_crm(deal_id, refresh=f"backfill-{self.clock.now().date()}"))
        deal = self._deal(deal_id)
        contact_email = self.conn.execute("SELECT email FROM contacts WHERE id = %s",
                                          (deal["contact_id"],)).fetchone()[0]
        for message_id, direction, subject, body, at in self.conn.execute(
                "SELECT message_id, direction, subject, body, created_at FROM messages WHERE deal_id = %s "
                "AND message_id IS NOT NULL ORDER BY id", (deal_id,)).fetchall():
            outbound = direction == "outbound"
            if self.log_email_to_crm(deal_id, direction, message_id, subject, body, at,
                                     self.sender_email if outbound else contact_email,
                                     contact_email if outbound else self.sender_email):
                queued += 1
        return queued

    def _crm_note(self, deal_id: UUID, key: str, text: str) -> None:
        if self.crm_sync:
            self._crm_action(deal_id, None, key, "crm_note", {"record_id": str(deal_id), "text": text})

    # --- replies from the mailbox -----------------------------------------------------------------
    def _deal_for_inbound(self, email: Any) -> tuple[UUID | None, str, list[UUID]]:
        """Which deal an inbound message belongs to, and how we know:
          1. thread: In-Reply-To / References name a message we sent (our id or the real one),
          2. tag: the plus address it was sent to carries a short deal id,
          3. sender: only if exactly one open outreach deal exists for that sender.
        More than one candidate is never guessed: (None, "ambiguous", candidates)."""
        ids = [i for i in (*(email.references or ()), email.in_reply_to) if i]
        if ids:
            rows = {r[0] for r in self.conn.execute(
                "SELECT DISTINCT deal_id FROM messages WHERE deal_id IS NOT NULL AND direction = 'outbound' "
                "AND (message_id = ANY(%s) OR real_message_id = ANY(%s))", (ids, ids))}
            if len(rows) == 1:
                return rows.pop(), "thread", []
            if rows:
                return None, "ambiguous", sorted(rows)
        tags = {m for a in (email.to_addrs or (email.to,)) for m in PLUS_TAG.findall(a or "")}
        if tags:
            rows = {r[0] for t in tags for r in self.conn.execute(
                "SELECT DISTINCT m.deal_id FROM messages m WHERE m.direction = 'outbound' AND "
                "m.deal_id::text LIKE %s", (t + "%",))}
            if len(rows) == 1:
                return rows.pop(), "tag", []
            if rows:
                return None, "ambiguous", sorted(rows)
        rows = [r[0] for r in self.conn.execute(
            "SELECT d.id FROM deals d JOIN contacts c ON c.id = d.contact_id "
            "WHERE regexp_replace(lower(c.email), '\\+[^@]*@', '@') = %s "
            "AND d.stage NOT IN ('closed_lost', 'do_not_contact', 'handoff') "
            "AND EXISTS (SELECT 1 FROM messages m WHERE m.deal_id = d.id AND m.direction = 'outbound') "
            "ORDER BY d.created_at", (base_of(email.from_addr),))]
        if len(rows) == 1:
            return rows[0], "sender", []
        return (None, "ambiguous", rows) if rows else (None, "none", [])

    def poll_replies(self) -> list[PipelineResult]:
        """Fetch replies (the inbox itself returns only replies to our messages or mail from an
        allowlisted prospect), route each to its deal, handle it, and mark it delivered."""
        if self.inbox is None:
            return []
        self.backfill_real_ids()
        results = []
        for email in self.inbox.poll():
            deal_id, how, candidates = self._deal_for_inbound(email)
            if deal_id is not None:
                write_audit(self.conn, "orchestrator", "inbound.matched", deal_id,
                            {"message_id": email.message_id, "by": how})
                results.append(self.handle_reply(deal_id, email))
            elif how == "ambiguous":
                self._hold_reply(email, candidates)
            else:
                write_audit(self.conn, "orchestrator", "inbound.unmatched", None,
                            {"message_id": email.message_id})
            self.inbox.mark_processed(email.message_id)       # only after it was handled
        return results

    def _hold_reply(self, email: Any, candidates: list[UUID]) -> None:
        """Several deals could own this reply: keep it as data for a person, attach it to none."""
        with self.conn.transaction():
            self.conn.execute(
                "INSERT INTO held_replies (message_id, from_addr, subject, body, received_at, reason, "
                "candidate_deal_ids) VALUES (%s, %s, %s, %s, %s, 'ambiguous', %s) "
                "ON CONFLICT (message_id) DO NOTHING",
                (email.message_id, email.from_addr, email.subject, email.body, email.received_at,
                 candidates))
            write_audit(self.conn, "orchestrator", "inbound.ambiguous", None, {
                "message_id": email.message_id, "candidates": [str(c) for c in candidates]})

    def backfill_real_ids(self) -> None:
        """Record the Message-ID Gmail really assigned to each email we sent (when the Sent copy was
        not visible yet at send time). A mailer that cannot say simply does nothing."""
        mailer = getattr(self.provider, "provider_for", lambda _t: None)("first_touch")
        reader = getattr(mailer, "actual_message_id", None)
        if reader is None:
            return
        for message_id, key in self.conn.execute(
                "SELECT m.message_id, a.idempotency_key FROM messages m JOIN actions a ON a.provider_id = "
                "m.message_id WHERE m.direction = 'outbound' AND m.real_message_id IS NULL "
                "ORDER BY m.id DESC LIMIT 20").fetchall():
            self._store_real_id(message_id, reader(key))

    def _store_real_id(self, message_id: str, real: str | None) -> None:
        if real:
            self.conn.execute("UPDATE messages SET real_message_id = %s WHERE message_id = %s",
                              (real, message_id))

    # --- sending ----------------------------------------------------------------------------------
    def send_action(self, action_id: UUID) -> GateResult:
        """Hand an approved action to the send gate. A first touch that goes out moves the deal."""
        row = self.conn.cursor(row_factory=dict_row).execute(
            "SELECT a.type, a.deal_id, d.segment_id FROM actions a JOIN deals d ON d.id = a.deal_id "
            "WHERE a.id = %s", (action_id,)).fetchone()
        pack = self.packs[row["segment_id"]]
        if row["type"] == "crm_update" and self._superseded_crm_write(action_id):
            return GateResult("refused", "a newer CRM write for this record replaces it")
        result = run_approved_action(self.conn, action_id, self.provider, self.clock,
                                     self.policy_config(pack))
        if result.status == "sent":
            self._record_sent_email(action_id, result.provider_id, pack)
            self._record_lesson_uses(action_id)
        if result.status == "sent":
            self._after_sent(row["type"], row["deal_id"], action_id, result.provider_id)
        elif result.status == "deferred" and result.run_at:
            record_event(self.conn, f"send:{action_id}:{result.run_at.isoformat()}", "action.send",
                         {"action_id": str(action_id)}, self.clock, run_at=result.run_at)
        self._notify(action_id)
        return result

    def _superseded_crm_write(self, action_id: UUID) -> bool:
        """A CRM write that waited out a retry must not overwrite a newer snapshot of the same
        record (the stage going back from outreach to enriched). Each snapshot holds the whole
        record, so a newer one replaces it; cancel the old one. Never touches mail. A logged email is not a
        snapshot: each one is its own event and is never replaced by a later one."""
        with self.conn.transaction():
            row = self.conn.execute(
                "UPDATE actions a SET status = 'cancelled' WHERE a.id = %s AND a.status = 'approved' "
                "AND a.payload->>'kind' <> 'emails' "
                "AND EXISTS (SELECT 1 FROM actions n WHERE n.deal_id = a.deal_id AND n.type = 'crm_update' "
                "AND n.payload->>'kind' = a.payload->>'kind' AND n.status IN ('approved', 'sent') "
                "AND (n.created_at, n.id) > (a.created_at, a.id)) RETURNING a.deal_id", (action_id,)
            ).fetchone()
            if row:
                self.conn.execute("INSERT INTO audit_log (actor, event, deal_id, detail) VALUES "
                                  "('orchestrator', 'crm.write_superseded', %s, %s)",
                                  (row[0], Jsonb({"action_id": str(action_id)})))
        return row is not None

    def _after_sent(self, kind: str, deal_id: UUID, action_id: UUID, provider_id: str | None) -> None:
        self.sync_to_crm(deal_id)
        if kind == "first_touch":
            # only the first time: a repeated send job must never move a deal backwards
            if DealStage(self._deal(deal_id)["stage"]) is DealStage.SCORED:
                self.advance(deal_id, DealStage.OUTREACH, "first touch sent")
        elif kind == "book_meeting":
            payload = self.conn.execute("SELECT payload FROM actions WHERE id = %s",
                                        (action_id,)).fetchone()[0]
            self.record_meeting_booked(deal_id, payload.get("start"))
        elif kind == "send_io":
            payload = self.conn.execute("SELECT payload FROM actions WHERE id = %s",
                                        (action_id,)).fetchone()[0]
            self._merge_fields(deal_id, {"envelope_id": provider_id, "io_total": payload.get("total")})
            self.advance(deal_id, DealStage.OUT_FOR_SIGNATURE, "sent for signature (test mode)")
        elif kind == "handoff":
            self.advance(deal_id, DealStage.HANDOFF, "handed to customer success")

    def _case_study_row(self, pack: Pack, slug: str) -> int:
        study = next(c for c in pack.case_studies if c.id == slug)
        return self.conn.execute(
            "INSERT INTO case_studies (segment_id, slug, title, tags, metric, source_url, label) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (segment_id, slug) DO UPDATE SET "
            "title = EXCLUDED.title, tags = EXCLUDED.tags, metric = EXCLUDED.metric, "
            "source_url = EXCLUDED.source_url RETURNING id",
            (pack.id, slug, study.customer, study.tags, study.metric_text, study.source_url,
             study.label)).fetchone()[0]

    def _record_lesson_uses(self, action_id: UUID) -> None:
        action = self.conn.cursor(row_factory=dict_row).execute(
            "SELECT deal_id, payload FROM actions WHERE id = %s", (action_id,)).fetchone()
        ids = [int(i) for i in action["payload"].get("lesson_ids", [])]
        if ids:
            lesson_loop.record_use(self.conn, ids, action["deal_id"], action_id, self.clock)
        if action["payload"].get("template") == "case_study_reply":     # that objection is answered
            self.conn.execute(
                "UPDATE deals SET fields = jsonb_set(fields, '{open_objections}', "
                "coalesce((SELECT jsonb_agg(v) FROM jsonb_array_elements(coalesce(fields->"
                "'open_objections', '[]'::jsonb)) v WHERE v <> '\"price_objection\"'::jsonb), "
                "'[]'::jsonb)) WHERE id = %s", (action["deal_id"],))

    def _read_back_real_id(self, atype: str, action_id: UUID, message_id: str) -> None:
        """Ask the mailer which Message-ID the Sent copy really has and keep it beside ours."""
        reader = getattr(getattr(self.provider, "provider_for", lambda _t: None)(atype),
                         "actual_message_id", None)
        if reader is None:
            return
        key = self.conn.execute("SELECT idempotency_key FROM actions WHERE id = %s",
                                (action_id,)).fetchone()[0]
        try:
            self._store_real_id(message_id, reader(key))
        except RevosError:
            pass                                  # not visible yet: backfilled at the next poll

    def _record_sent_email(self, action_id: UUID, provider_id: str | None, pack: Pack) -> None:
        """Keep our own copy of what went out, and which case study (if any) it carried."""
        action = self.conn.cursor(row_factory=dict_row).execute(
            "SELECT type, deal_id, contact_id, payload FROM actions WHERE id = %s",
            (action_id,)).fetchone()
        if action["type"] not in {"first_touch", "follow_up"} or not provider_id:
            return
        payload = action["payload"]
        with self.conn.transaction():
            self.conn.execute(
                "INSERT INTO messages (deal_id, contact_id, direction, message_id, in_reply_to, "
                "subject, body) VALUES (%s, %s, 'outbound', %s, %s, %s, %s) "
                "ON CONFLICT (message_id) WHERE message_id IS NOT NULL DO NOTHING",
                (action["deal_id"], action["contact_id"], provider_id, payload.get("in_reply_to"),
                 payload.get("subject"), payload.get("body")))
            self._read_back_real_id(action["type"], action_id, provider_id)
            to = self.conn.execute("SELECT email FROM contacts WHERE id = %s",
                                   (action["contact_id"],)).fetchone()
            self.log_email_to_crm(action["deal_id"], "outbound", provider_id, payload.get("subject"),
                                  payload.get("body"), self.clock.now(), self.sender_email,
                                  to[0] if to else "")
            slug = payload.get("case_study_id")
            if slug:
                message = self.conn.execute("SELECT id FROM messages WHERE message_id = %s",
                                            (provider_id,)).fetchone()
                self.conn.execute(
                    "INSERT INTO case_study_sends (case_study_id, deal_id, message_id, action_id) "
                    "VALUES (%s, %s, %s, %s) ON CONFLICT (action_id) DO NOTHING",
                    (self._case_study_row(pack, slug), action["deal_id"],
                     message[0] if message else None, action_id))

    def handlers(self) -> dict[str, Callable[[psycopg.Connection, Any], None]]:
        """Job handlers for the worker."""
        def lead_created(_conn: psycopg.Connection, job: Any) -> None:
            self.handle_lead_created(UUID(job.payload["deal_id"]))

        def send(_conn: psycopg.Connection, job: Any) -> None:
            self.send_action(UUID(job.payload["action_id"]))

        def veto_expired(_conn: psycopg.Connection, job: Any) -> None:
            released = release_veto_expired(self.conn, self.clock)
            for action_id in released:
                self._notify(action_id)
            # Also the action this timer is for: if a first attempt to send it failed (mail was down) the
            # window is already released, so only this retry can still send it. send_action is idempotent.
            own = [UUID(job.payload["action_id"])] if job.payload.get("action_id") else []
            for action_id in dict.fromkeys([*released, *own]):
                self.send_action(action_id)

        def snooze_ended(_conn: psycopg.Connection, job: Any) -> None:
            self.resume_after_snooze(UUID(job.payload["deal_id"]))

        def release_held(_conn: psycopg.Connection, job: Any) -> None:
            release_held_sends(self.conn, self.clock)

        def lessons_sweep(_conn: psycopg.Connection, job: Any) -> None:
            self.sweep_lessons()

        def poll(_conn: psycopg.Connection, job: Any) -> None:
            self.poll_replies()

        def signature_event(_conn: psycopg.Connection, job: Any) -> None:
            if job.payload.get("kind") == "all_signed":
                self.complete_signature(job.payload["envelope_id"])
            else:                                       # sent, viewed, signed: recorded, nothing else moves
                self.note_signature_event(job.payload["envelope_id"], job.payload.get("kind", "other"),
                                          job.payload.get("event_type", ""))

        def slack_sync(_conn: psycopg.Connection, job: Any) -> None:
            if self.notifier is not None:
                self.notifier.sync(UUID(job.payload["action_id"]))

        def slack_call(_conn: psycopg.Connection, job: Any) -> None:
            if self.notifier is not None:
                self.notifier.post_call_summary(UUID(job.payload["deal_id"]), job.payload["transcript_id"],
                                                job.payload["title"])

        def calls_poll(_conn: psycopg.Connection, job: Any) -> None:
            self.poll_calls()

        def stall_scan(_conn: psycopg.Connection, job: Any) -> None:
            sweeps.stall_scan(self.conn, self.clock)

        def digest(_conn: psycopg.Connection, job: Any) -> None:
            sweeps.digest(self.conn, self.clock, self.notifier)

        return {"slack.call": slack_call, "calls.poll": calls_poll, "slack.sync": slack_sync,
                "tracking.stall_scan": stall_scan, "tracking.digest": digest,
                "email.poll": poll,
                "signature.event": signature_event, "lead.created": lead_created,
                "action.send": send, "lessons.sweep": lessons_sweep,
                "action.veto_expired": veto_expired, "lead.snooze_ended": snooze_ended,
                "queue.release_held": release_held}

