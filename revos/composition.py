"""Composition root: the only place that picks concrete adapters."""

import os

import psycopg

from revos.adapters.claude_llm import ClaudeLLM
from revos.adapters.fake_llm import FakeLLM
from revos.modes import (
    dropbox_sign_configured,
    fireflies_configured,
    gmail_keys,
    hubspot_configured,
    slack_configured,
)
from revos.orchestrator.resets import CLOSED
from revos.ports.llm import LLM


def build_llm(conn: psycopg.Connection | None = None) -> LLM:
    """LLM_MODE=fake (default) is the offline model; LLM_MODE=live is Claude with a spend cap."""
    mode = os.environ.get("LLM_MODE", "fake")
    if mode == "fake":
        return FakeLLM()
    if mode == "live":
        if conn is None:
            raise ValueError("live mode needs a database connection for the spend ledger")
        return ClaudeLLM(conn)
    raise ValueError(f"LLM_MODE must be 'fake' or 'live', got '{mode}'")


def build_runner(conn: psycopg.Connection | None = None):
    """The agent runner. Which one is active is recorded on every run and in the audit log.

    * LLM_MODE=fake (default): RulesRunner, the deterministic fake model. No key, no network.
    * LLM_MODE=live: SdkRunner on the Claude Agent SDK. Needs ANTHROPIC_API_KEY and a database
      connection for the spend cap. If either is missing this fails loudly; it never quietly falls
      back to the deterministic runner.
    """
    from revos.adapters.claude_llm import ClaudeConfig
    from revos.adapters.spend import SpendLedger
    from revos.agents.runner import RulesRunner, SdkRunner

    mode = os.environ.get("LLM_MODE", "fake")
    if mode == "fake":
        return RulesRunner(FakeLLM())
    if mode == "live":
        if not os.environ.get("ANTHROPIC_API_KEY", "").strip():
            raise ValueError("LLM_MODE=live needs ANTHROPIC_API_KEY; refusing to fall back to the "
                             "deterministic runner")
        if conn is None:
            raise ValueError("live mode needs a database connection for the spend ledger")
        config = ClaudeConfig.from_env()
        return SdkRunner(SpendLedger(conn, config.max_spend_usd, config.reserve_per_call_usd),
                         ClaudeLLM(conn, config), config)
    raise ValueError(f"LLM_MODE must be 'fake' or 'live', got '{mode}'")


def build_crm():
    """The CRM adapter: HubSpot when HUBSPOT_TOKEN is set, otherwise the mock."""
    from revos.adapters.hubspot.client import DEFAULT_BASE_URL, HubSpotClient
    from revos.adapters.hubspot.crm import HubSpotCRM
    from revos.adapters.mock.crm import MockCRM

    if not hubspot_configured():
        return MockCRM()
    token = os.environ["HUBSPOT_TOKEN"].strip()
    base = os.environ.get("HUBSPOT_BASE_URL", "").strip() or DEFAULT_BASE_URL
    return HubSpotCRM(HubSpotClient(token, base))


def build_slack():
    """(slack, channel): the real adapter when SLACK_BOT_TOKEN is set, otherwise the mock."""
    from revos.adapters.mock.slack import MockSlack
    from revos.adapters.slack.client import SlackAdapter

    channel = os.environ.get("SLACK_CHANNEL", "").strip() or "#revops"
    if not slack_configured():
        return MockSlack(), channel
    return SlackAdapter(os.environ["SLACK_BOT_TOKEN"].strip()), channel


def build_transcripts(world_transcripts):
    """The transcript source: Fireflies when FIREFLIES_API_KEY is set, otherwise the mock."""
    from revos.adapters.fireflies.client import FirefliesAdapter

    if not fireflies_configured():
        return world_transcripts
    return FirefliesAdapter(os.environ["FIREFLIES_API_KEY"].strip())


def build_esign(world_esign=None):
    """The e-signature provider: Dropbox Sign (TEST MODE ONLY) when its key is set, else the mock."""
    from revos.adapters.dropboxsign.client import DropboxSignAdapter
    from revos.adapters.mock.esign import MockESign

    if not dropbox_sign_configured():
        return world_esign if world_esign is not None else MockESign()
    return DropboxSignAdapter(os.environ["DROPBOX_SIGN_API_KEY"].strip())


def build_identity():
    """Who the emails are from. Real mail needs a real postal address (see build_mail)."""
    from revos.agents.types import Identity

    defaults = Identity()
    return Identity(
        sender_name=os.environ.get("SENDER_NAME", "").strip() or defaults.sender_name,
        unsubscribe_line=os.environ.get("UNSUBSCRIBE_LINE", "").strip() or defaults.unsubscribe_line,
        postal_address=os.environ.get("POSTAL_ADDRESS", "").strip() or defaults.postal_address)


def gmail_configured() -> bool:
    return gmail_keys()


def build_mail(conn: psycopg.Connection):
    """(mailer, inbox): Gmail when the credentials exist, otherwise the mocks.

    Real mail refuses to start without a postal address: every email must carry one."""
    from revos.adapters.gmail.inbox import GmailInbox
    from revos.adapters.gmail.mailer import GmailMailer
    from revos.adapters.gmail.transport import ImapMailbox, SmtplibTransport
    from revos.adapters.mock.mailer import MockInbox, MockMailer

    if not gmail_configured():
        return MockMailer(), MockInbox()
    if not os.environ.get("POSTAL_ADDRESS", "").strip():
        raise ValueError("POSTAL_ADDRESS is required for real email: every message carries it")
    address, password = os.environ["GMAIL_ADDRESS"].strip(), os.environ["GMAIL_APP_PASSWORD"].strip()
    identity = build_identity()
    prospect = os.environ.get("PROSPECT_TEST_EMAIL", "").strip().lower()
    demo = os.environ.get("REVOS_ENV", "demo") == "demo"
    allowlist = frozenset({address.lower(), prospect} - {""}) if demo else None
    mailbox = ImapMailbox(address, password)
    mailer = GmailMailer(address, smtp=SmtplibTransport(address, password), mailbox=mailbox,
                         allowlist=allowlist, plus_bases=frozenset({prospect} - {""}),
                         required_footer=(identity.unsubscribe_line, identity.postal_address),
                         sender_name=identity.sender_name)

    def our_ids() -> list[str]:
        # ours, and the id Gmail really gave each Sent copy when it differs
        # only deals still open: every id costs one IMAP session, and a closed deal's thread needs no polling
        return [i for r in conn.execute(
            "SELECT m.message_id, m.real_message_id FROM messages m JOIN deals d ON d.id = m.deal_id "
            "WHERE m.direction = 'outbound' AND m.message_id IS NOT NULL AND d.stage <> ALL(%s) "
            "ORDER BY m.id DESC LIMIT 100", (list(CLOSED),)) for i in r if i]

    def first_sent():
        return conn.execute("SELECT min(created_at) FROM messages WHERE direction = 'outbound'"
                            ).fetchone()[0]

    inbox = GmailInbox(mailbox, our_message_ids=our_ids, not_before=first_sent,
                       allowed_senders=frozenset({prospect} - {""}))
    return mailer, inbox


def build_orchestrator(conn: psycopg.Connection, ro_conn: psycopg.Connection, clock=None):
    """The composition root for the running system: concrete adapters where credentials exist,
    mocks everywhere else. The same Orchestrator either way."""
    from pathlib import Path

    from revos.adapters.clock import SystemClock
    from revos.adapters.mock import build_mock_world
    from revos.adapters.router import ActionRouter
    from revos.orchestrator.pipeline import Orchestrator
    from revos.policy.config import PolicyConfig
    from revos.segments import load_all
    from revos.slack.notifier import SlackNotifier

    world = build_mock_world()
    mailer, inbox = build_mail(conn)
    crm = build_crm()
    policy = PolicyConfig.from_env()
    from revos.adapters.esign_provider import ESignOutboxProvider
    from revos.adapters.slack.handoff import SlackHandoff
    from revos.db import connect

    slack, channel = build_slack()
    esign = build_esign(world.esign)
    router = ActionRouter({"first_touch": mailer, "follow_up": mailer, "crm_update": crm,
                           "crm_note": crm, "send_io": ESignOutboxProvider(esign, connect),
                           "book_meeting": world.calendar, "handoff": SlackHandoff(slack, channel, connect)})
    packs = load_all(Path(__file__).resolve().parent.parent / "packs")
    clock = clock or SystemClock()
    return Orchestrator(
        conn, ro_conn, clock=clock, packs=packs, runner=build_runner(conn),
        research=world.research, provider=router, identity=build_identity(),
        demo_mode=policy.demo_mode, allowed_recipients=policy.allowed_recipients,
        plus_bases=policy.plus_bases, send_window_bypass=policy.send_window_bypass,
        domain_cap_bypass=policy.domain_cap_bypass,
        notifier=SlackNotifier(conn, slack, channel, clock, packs),
        veto_window_seconds=(int(os.environ["VETO_WINDOW_SECONDS"])
                             if os.environ.get("VETO_WINDOW_SECONDS", "").strip() else None),
        calendar=world.calendar, transcripts=build_transcripts(world.transcripts), esign=esign,
        inbox=inbox, crm_sync=True, daily_cap=int(os.environ.get("DAILY_SEND_CAP", "20")))
