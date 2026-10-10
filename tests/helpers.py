from dataclasses import replace

from revos.domain.enums import Tier
from revos.policy.config import PolicyConfig
from revos.policy.engine import ActionRequest
from revos.policy.service import propose_action

# Monday 2026-10-12 09:00 UTC is the FakeClock start.
CFG = PolicyConfig(
    demo_mode=True,
    allowed_recipients=frozenset({"owner@example.test", "sender@example.test", "other@example.test",
                                  "a@shared.test", "b@shared.test", "c@shared.test"}),
)


def make_contact(conn, email="owner@example.test", tz=None, suppressed=False):
    return conn.execute(
        "INSERT INTO contacts (email, timezone, suppressed, suppressed_at) "
        "VALUES (%s, %s, %s, CASE WHEN %s THEN now() END) RETURNING id",
        (email, tz, suppressed, suppressed),
    ).fetchone()[0]


def good_first_touch(**over) -> ActionRequest:
    base = ActionRequest("first_touch", tier=Tier.B, body="Hello, saw your campaign.",
                         claim_check_passed=True)
    return replace(base, **over)


def propose(conn, clock, key, request, contact=None, config=CFG):
    return propose_action(conn, clock, config, key=key, request=request, contact_id=contact)


def status(conn, action_id):
    return conn.execute(
        "SELECT status, decision, hold_reason, run_at FROM actions WHERE id = %s", (action_id,)
    ).fetchone()
