"""The orchestrator: runs agents, then decides what their proposals become.

The only code that turns a proposal into anything. For each proposal it recomputes what it can
from the database, asks the policy engine, creates the outbox action, and applies internal
effects. It is also the only caller of transition_deal for agent-driven moves.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta
from typing import Any
from uuid import UUID

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from revos.agents.broker import ToolBroker
from revos.agents.common import check_inputs, evidence_from_fields, lead_facts
from revos.agents.registry import AGENTS
from revos.agents.runner import AgentRunner
from revos.agents.store import RunRow, RunStore
from revos.agents.types import AgentEnv, Identity
from revos.domain.drafting import Claim, Draft
from revos.domain.enums import Tier
from revos.domain.errors import (
    ExitCriteriaNotMet,
    LLMError,
    LoopLimitExceeded,
    ProposalRejected,
    QuarantinedContent,
    SpendCapExceeded,
    ToolCallBlocked,
)
from revos.domain.stages import DealStage, can_transition
from revos.domain.untrusted import UntrustedText
from revos.mcp.registry import BlockEvent, ToolContext
from revos.mcp.scope import fixed_scope
from revos.orchestrator.transitions import transition_deal
from revos.policy.audit import write_audit
from revos.policy.config import PolicyConfig
from revos.policy.engine import ActionRequest
from revos.policy.gate import GateResult, run_approved_action
from revos.policy.service import propose_action, release_veto_expired
from revos.policy.settings import release_held_sends
from revos.ports.calendar import Calendar, Slot
from revos.ports.clock import Clock
from revos.ports.provider import ActionProvider
from revos.ports.research import Research
from revos.ports.transcripts import Transcripts
from revos.runtime import lessons as lesson_loop
from revos.runtime.events import record_event
from revos.runtime.quarantine import record_quarantine
from revos.segments.loader import Pack
from revos.skills.claims import deterministic_check
from revos.skills.io import validate_io
from revos.skills.scoring import score_lead

# A run that ends one of these ways goes to a person, with an audit entry. Order matters.
HUMAN_CAUSES: tuple[tuple[type[Exception], str], ...] = (
    (SpendCapExceeded, "spend_cap_exceeded"), (QuarantinedContent, "quarantined"),
    (LoopLimitExceeded, "loop_limit"), (ToolCallBlocked, "tool_blocked"),
    (LLMError, "llm_error"), (ExitCriteriaNotMet, "exit_criteria_not_met"),
    (ProposalRejected, "proposal_rejected"),
)
INTERNAL = {"enrich", "score", "classify_reply", "suppress_contact", "cancel_scheduled_sends",
            "snooze_ooo", "reminder", "attach_case_study", "generate_io", "lesson_write"}
SENDABLE = {"first_touch", "follow_up", "send_io", "crm_update", "crm_note"}
FIELD_PREFIXES = ("fact:", "fact_source:", "quote:")
EXTRA_FIELDS = {"company_domain", "decision_maker_name", "decision_maker_role"}
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
                 learn_from_replies: bool = False) -> None:
        self.conn, self.ro_conn, self.clock, self.packs = conn, ro_conn, clock, packs
        self.runner, self.research, self.provider = runner, research, provider
        self.identity = identity or Identity()
        self.demo_mode, self.allowed_recipients, self.calendar = demo_mode, allowed_recipients, calendar
        self.transcripts, self.learn_from_replies = transcripts, learn_from_replies
        self.store = RunStore(conn)

    def _slots(self) -> tuple[Slot, ...]:
        if self.calendar is None:
            return ()
        now = self.clock.now()
        return tuple(self.calendar.free_slots(now + timedelta(days=1), now + timedelta(days=8), 30)[:3])

    def _last_outbound_subject(self, deal_id: UUID) -> str | None:
        row = self.conn.execute("SELECT subject FROM messages WHERE deal_id = %s AND direction = "
                                "'outbound' ORDER BY id DESC LIMIT 1", (deal_id,)).fetchone()
        return row[0] if row else None

    # --- reading ----------------------------------------
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
        return PolicyConfig.from_pack(pack, demo_mode=self.demo_mode,
                                      allowed_recipients=self.allowed_recipients)

    def _tool_ctx(self, deal_id: UUID) -> ToolContext:
        def blocked(event: BlockEvent) -> None:
            write_audit(self.conn, "tools", "tool.blocked", event.deal_id or deal_id,
                        {"agent": event.agent, "tool": event.tool, "reason": event.reason})

        def quarantined(content: UntrustedText, deal: UUID) -> None:
            record_quarantine(self.conn, content, deal)

        return ToolContext(self.ro_conn, self.packs, self.research, fixed_scope(self.ro_conn, deal_id),
                           blocked, quarantined)

    # --- running an agent ----------------------------------------
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

    # --- applying proposals ----------------------------------------
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
            proposal = propose_action(
                self.conn, self.clock, self.policy_config(pack), key=key, request=request,
                deal_id=deal_id, contact_id=UUID(contact_id) if contact_id else None,
                payload=payload)
            action = self.conn.cursor(row_factory=dict_row).execute(
                "SELECT id, status, hold_reason, veto_until FROM actions WHERE id = %s",
                (proposal.action_id,)).fetchone()
            approved = action["status"] == "approved"
            if atype in INTERNAL and approved:
                self._effect(atype, deal, pack, payload, run)
            elif atype in SENDABLE and action["status"] in {"approved", "pending"}:
                self._queue_send(action, key)
            if payload.get("lesson_ids") and proposal.created:
                write_audit(self.conn, "orchestrator", "lesson.cited", deal_id, {
                    "action_id": str(action["id"]), "action_type": atype,
                    "lesson_ids": payload["lesson_ids"], "angle": payload.get("angle"),
                    "run": run.step})
            outcome = (proposal.decision.outcome.value if proposal.decision
                       else f"existing:{action['status']}")
            rejected = action["status"] == "cancelled"
            self.conn.execute(
                "UPDATE agent_proposals SET status = %s, action_id = %s, outcome = %s "
                "WHERE run_id = %s AND idx = %s",
                ("rejected" if rejected else "applied", action["id"], outcome, run.run_id, p["idx"]))
        return {"idx": p["idx"], "action_type": atype, "action_id": action["id"],
                "status": action["status"], "outcome": outcome}

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
            check = validate_io(pack, fields)
            if not check.ok:      # recomputed from the database; the agent's word is not enough
                raise ProposalRejected("the IO does not validate: " + "; ".join(check.problems))
            return ActionRequest(atype, io_validated=check.ok,
                                 discount_pct=float(fields.get("discount_pct", 0) or 0))
        if atype == "attach_case_study":
            return ActionRequest(atype, from_library=any(
                c.id == payload.get("case_study_id") for c in pack.case_studies))
        return ActionRequest(atype)

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
        approved, names = check_inputs(pack, template, self.identity, deal["company_name"] or "",
                                       deal["contact_name"], study, extra)
        draft = Draft(payload["subject"], payload["body"], tuple(
            Claim(c["text"], tuple(c["evidence"])) for c in payload.get("claims", [])))
        issues = deterministic_check(draft, evidence_from_fields(deal["fields"] or {}), approved, names)
        return not issues

    def _queue_send(self, action: Mapping[str, Any], key: str) -> None:
        if action["status"] == "approved":
            record_event(self.conn, f"send:{key}", "action.send", {"action_id": str(action["id"])},
                         self.clock)
        elif action["veto_until"] is not None:
            record_event(self.conn, f"veto:{key}", "action.veto_expired",
                         {"action_id": str(action["id"])}, self.clock, run_at=action["veto_until"])

    # --- internal effects ----------------------------------------
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
            self._learn_from_reply(deal, payload["label"])
        elif atype in {"suppress_contact", "cancel_scheduled_sends"}:
            self._opt_out(deal, atype, payload)
        elif atype == "attach_case_study":
            self._merge_fields(deal_id, {"proposed_case_study": payload["case_study_id"]})
        elif atype == "generate_io":
            self.conn.execute(
                "INSERT INTO documents (deal_id, kind, version, body, validation) VALUES "
                "(%s, 'io', 1, %s, %s) ON CONFLICT (deal_id, kind, version) DO UPDATE SET "
                "body = EXCLUDED.body, validation = EXCLUDED.validation",
                (deal_id, payload["document"], Jsonb({"ok": payload["ok"],
                                                       "problems": payload["problems"],
                                                       "total": payload["total"]})))
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

    # --- stage moves: only here ----------------------------------------
    def advance(self, deal_id: UUID, to_stage: DealStage, reason: str) -> bool:
        deal = self._deal(deal_id)
        current = DealStage(deal["stage"])
        if current == to_stage or not can_transition(current, to_stage):
            return False
        transition_deal(self.conn, deal_id, to_stage, actor="orchestrator", reason=reason,
                        exit_criteria=self._pack(deal).stages.exit_criteria)
        return True

    # --- the slice: lead.created -> enrichment -> scoring -> outreach proposal -----------------------------
    def handle_lead_created(self, deal_id: UUID) -> PipelineResult:
        runs: list[tuple[str, str]] = []

        def stop(status: str, reason: str | None, **kw: Any) -> PipelineResult:
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

    # --- a call: transcript -> deal fields -> lessons --------------------------------------------------
    def handle_transcript(self, deal_id: UUID, transcript_id: str) -> PipelineResult:
        """Turn a finished call into deal fields (each with its quote) and lessons (each with its
        evidence). A transcript with an instruction in it is quarantined and teaches nothing."""
        if self.transcripts is None:
            raise LookupError("no transcript source is configured")
        transcript = self.transcripts.get(transcript_id)
        if transcript is None:
            raise LookupError(f"no transcript {transcript_id}")
        text = UntrustedText.wrap("transcript", transcript.id, transcript.text)
        runs: list[tuple[str, str]] = []

        def done(status: str, reason: str | None = None) -> PipelineResult:
            if status == "needs_human":
                write_audit(self.conn, "orchestrator", "pipeline.needs_human", deal_id,
                            {"runner": self.runner.name, "reason": reason, "transcript": transcript_id})
            return PipelineResult(status, self._deal(deal_id)["stage"], self.runner.name, reason,
                                  runs=tuple(runs))

        try:
            run = self.run_agent(deal_id, "call_intel", f"call:{transcript_id}",
                                 untrusted={"transcript": text})
            runs.append(("call_intel", run.status))
            if run.status != "completed":
                return done("needs_human", run.human_reason)
            self.apply_run(run, deal_id)
            quotes = run.proposals[0]["payload"].get("quotes", {}) if run.proposals else {}
            for name in ("objections", "needs"):
                if name in quotes:
                    self._learn(deal_id, f"know:{transcript_id}:{name}", quotes[name],
                                f"{transcript_id}#{name}", "call", runs)
            return done("completed")
        except tuple(cls for cls, _ in HUMAN_CAUSES) as exc:
            return done("needs_human", f"{cause_of(exc)}: {exc}")

    def _learn(self, deal_id: UUID, step: str, quote: str, origin: str, source: str,
               runs: list[tuple[str, str]]) -> None:
        candidate = UntrustedText.wrap("transcript", origin, quote)
        run = self.run_agent(deal_id, "knowledge", step, input_={"source": source},
                             untrusted={"candidate": candidate})
        runs.append(("knowledge", run.status))
        if run.status == "completed":
            self.apply_run(run, deal_id)

    def record_meeting_booked(self, deal_id: UUID) -> list[lesson_loop.LessonChange]:
        """A meeting is booked: the best outcome for every lesson used on this deal so far."""
        changes = lesson_loop.record_outcome(self.conn, deal_id, "meeting", self.clock)
        self.advance(deal_id, DealStage.MEETING, "a meeting was booked")
        return changes

    def sweep_lessons(self) -> list[lesson_loop.LessonChange]:
        return lesson_loop.sweep_no_replies(self.conn, self.clock)

    # --- a customer reply ---------------------------------------------------------------------------------
    def handle_reply(self, deal_id: UUID, email: Any) -> PipelineResult:
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
        runs: list[tuple[str, str]] = []

        def done(status: str, reason: str | None = None, label: str | None = None,
                 **kw: Any) -> PipelineResult:
            if status == "needs_human":
                write_audit(self.conn, "orchestrator", "pipeline.needs_human", deal_id,
                            {"runner": self.runner.name, "reason": reason, "label": label})
            return PipelineResult(status, self._deal(deal_id)["stage"], self.runner.name, reason,
                                  label=label, runs=tuple(runs), **kw)

        try:
            run = self.run_agent(
                deal_id, "reply", f"reply:{email.message_id}",
                input_={"message_id": email.message_id,
                        "original_subject": self._last_outbound_subject(deal_id)},
                untrusted={"reply": reply})
            runs.append(("reply", run.status))
            if run.status != "completed":
                return done("needs_human", run.human_reason)
            self.apply_run(run, deal_id)
            label = next((p["payload"]["label"] for p in run.proposals
                          if p["action_type"] == "classify_reply"), None)
            if run.output.get("stage_suggestion"):
                self.advance(deal_id, DealStage(run.output["stage_suggestion"]), f"reply: {label}")
            if label == "interested":                       # route: offer meeting times
                meeting = self.run_agent(deal_id, "meeting", f"meeting:{email.message_id}",
                                         slots=self._slots())
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

    # --- sending ----------------------------------------
    def send_action(self, action_id: UUID) -> GateResult:
        """Hand an approved action to the send gate. A first touch that goes out moves the deal."""
        row = self.conn.cursor(row_factory=dict_row).execute(
            "SELECT a.type, a.deal_id, d.segment_id FROM actions a JOIN deals d ON d.id = a.deal_id "
            "WHERE a.id = %s", (action_id,)).fetchone()
        pack = self.packs[row["segment_id"]]
        result = run_approved_action(self.conn, action_id, self.provider, self.clock,
                                     self.policy_config(pack))
        if result.status == "sent":
            self._record_sent_email(action_id, result.provider_id, pack)
            self._record_lesson_uses(action_id)
        if result.status == "sent" and row["type"] == "first_touch":
            self.advance(row["deal_id"], DealStage.OUTREACH, "first touch sent")
        elif result.status == "deferred" and result.run_at:
            record_event(self.conn, f"send:{action_id}:{result.run_at.isoformat()}", "action.send",
                         {"action_id": str(action_id)}, self.clock, run_at=result.run_at)
        return result

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
            for action_id in release_veto_expired(self.conn, self.clock):
                self.send_action(action_id)

        def snooze_ended(_conn: psycopg.Connection, job: Any) -> None:
            self.resume_after_snooze(UUID(job.payload["deal_id"]))

        def release_held(_conn: psycopg.Connection, job: Any) -> None:
            release_held_sends(self.conn, self.clock)

        def lessons_sweep(_conn: psycopg.Connection, job: Any) -> None:
            self.sweep_lessons()

        return {"lead.created": lead_created, "action.send": send, "lessons.sweep": lessons_sweep,
                "action.veto_expired": veto_expired, "lead.snooze_ended": snooze_ended,
                "queue.release_held": release_held}

