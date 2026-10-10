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


# --- Step 5 helpers: drafting against the real pack's template --------------------------------

from revos.domain.drafting import Evidence, EvidenceItem  # noqa: E402
from revos.ports.llm import DraftRequest  # noqa: E402

CONTEXT = {
    "company": "Acme Optical", "first_name": "Sam", "sender_name": "Jo Rivers",
    "unsubscribe_line": "Unsubscribe: reply STOP", "postal_address": "1 Example Street, Sampletown",
}
EVIDENCE = Evidence.of([
    EvidenceItem("F1", "Acme Optical operates 120 locations", "https://acme.example/about"),
    EvidenceItem("F2", "Acme Optical opened a Boise office in March", "https://acme.example/news"),
])


def first_touch_setup(pack, *, evidence=EVIDENCE, case_study_id="global_beauty_brand"):
    """(request, approved_texts, allowed_names) for a first touch built from the real pack."""
    template = pack.messaging.templates["first_touch"]
    study = next(c for c in pack.case_studies if c.id == case_study_id) if case_study_id else None
    pillar = pack.messaging.pillars[0]
    context = {**CONTEXT, "pillar": pillar, "case_study": study}
    request = DraftRequest(template.subject, template.body, context, evidence)
    approved = [template.subject, template.body,
                *(p.headline + " " + p.body for p in pack.messaging.pillars)]
    names = list(CONTEXT.values())
    if study:
        approved += [study.customer, study.summary, study.metric_text, study.source_url, study.label]
        names.append(study.customer)
    return request, approved, names
