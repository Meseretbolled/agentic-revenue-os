"""Table-driven test of PROJECT_BRIEF.md section 6.5, in both autonomy modes."""

from dataclasses import replace

import pytest

from revos.domain.enums import AutonomyLevel, AutonomyMode, Outcome, Tier
from revos.policy.config import DEFAULT_LEVELS, PolicyConfig
from revos.policy.engine import ActionRequest, decide, mentions_price

A, M = AutonomyMode.AUTONOMOUS, AutonomyMode.APPROVAL
OUT = Outcome


def req(kind: str, **kw) -> ActionRequest:
    return ActionRequest(kind, **kw)


def email(kind="first_touch", tier=Tier.B, body="Hi there.", **kw) -> ActionRequest:
    return ActionRequest(kind, tier=tier, body=body, claim_check_passed=True, **kw)


# (brief row, request, expected in autonomous mode, expected in approval mode)
TABLE = [
    ("enrich", req("enrich"), OUT.AUTO, OUT.AUTO),
    ("score", req("score"), OUT.AUTO, OUT.AUTO),
    ("crm field update", req("crm_update"), OUT.AUTO, OUT.AUTO),
    ("crm note", req("crm_note"), OUT.AUTO, OUT.AUTO),
    ("first touch tier B", email(tier=Tier.B), OUT.AUTO, OUT.AUTO),
    ("first touch tier C", email(tier=Tier.C), OUT.AUTO, OUT.AUTO),
    ("first touch tier A", email(tier=Tier.A), OUT.AUTO_WITH_VETO, OUT.NEEDS_APPROVAL),
    ("price inside rate card", email(body="Our CPM rates start low.", rate_card_ok=True),
     OUT.AUTO_WITH_VETO, OUT.NEEDS_APPROVAL),
    ("price outside rate card", email(body="We can do a 40% discount.", rate_card_ok=False),
     OUT.NEEDS_APPROVAL, OUT.NEEDS_APPROVAL),
    ("price, rate card unknown", email(body="The price is flexible."),
     OUT.NEEDS_APPROVAL, OUT.NEEDS_APPROVAL),
    ("follow-up", email(kind="follow_up"), OUT.AUTO, OUT.AUTO),
    ("reminder", req("reminder"), OUT.AUTO, OUT.AUTO),
    ("reply classification", req("classify_reply"), OUT.AUTO, OUT.AUTO),
    ("out-of-office snooze", req("snooze_ooo"), OUT.AUTO, OUT.AUTO),
    ("unsubscribe: suppress", req("suppress_contact"), OUT.AUTO, OUT.AUTO),
    ("unsubscribe: cancel sends", req("cancel_scheduled_sends"), OUT.AUTO, OUT.AUTO),
    ("case study from library", req("attach_case_study", from_library=True), OUT.AUTO, OUT.AUTO),
    ("generate IO draft", req("generate_io"), OUT.AUTO, OUT.AUTO),
    ("send IO, validated", req("send_io", io_validated=True), OUT.AUTO, OUT.AUTO),
    ("send IO, validated, discount at max", req("send_io", io_validated=True, discount_pct=15),
     OUT.AUTO, OUT.AUTO),
    ("social message", req("social_message"), OUT.DRAFT_ONLY, OUT.DRAFT_ONLY),
]


@pytest.mark.parametrize("name,request_,autonomous,approval", TABLE, ids=[t[0] for t in TABLE])
def test_autonomy_table(name, request_, autonomous, approval) -> None:
    config = PolicyConfig()
    assert decide(request_, config, mode=A, kill_switch=False).outcome == autonomous
    assert decide(request_, config, mode=M, kill_switch=False).outcome == approval


def test_every_allowlisted_action_is_covered_by_the_table() -> None:
    covered = {t[1].action_type for t in TABLE}
    listed = {k.split(".")[0] for k in DEFAULT_LEVELS} - {"price_message"}
    assert listed <= covered


# Rows that are about "blocked / denied" ---------------------------------------------------

@pytest.mark.parametrize("mode", [A, M])
def test_unknown_action_type_is_denied(mode) -> None:
    for kind in ("wire_transfer", "send_email", "delete_everything", ""):
        d = decide(req(kind), PolicyConfig(), mode=mode, kill_switch=False)
        assert d.outcome is OUT.DENIED and "allowlist" in d.reasons[0]


def test_case_study_outside_the_library_is_denied() -> None:
    d = decide(req("attach_case_study", from_library=False), PolicyConfig(), mode=A, kill_switch=False)
    assert d.outcome is OUT.DENIED


def test_first_touch_without_a_known_tier_is_denied() -> None:
    for tier in (None, Tier.DISQUALIFIED):
        d = decide(email(tier=tier), PolicyConfig(), mode=A, kill_switch=False)
        assert d.outcome is OUT.DENIED


HARD_BLOCKS = [
    ("claim check failed", replace(email(), claim_check_passed=False), "claim_check_failed"),
    ("claim check never run", replace(email(), claim_check_passed=None), "claim_check_failed"),
    ("injection flagged", email(injection_flagged=True), "injection_flagged"),
    ("suppressed contact", email(contact_suppressed=True), "suppressed"),
    ("discount over maximum", req("send_io", io_validated=True, discount_pct=15.01),
     "discount_over_maximum"),
    ("IO not validated", req("send_io", io_validated=False), "io_validation_failed"),
    ("IO validation unknown", req("send_io"), "io_validation_failed"),
]


@pytest.mark.parametrize("mode", [A, M])
@pytest.mark.parametrize("name,request_,reason", HARD_BLOCKS, ids=[h[0] for h in HARD_BLOCKS])
def test_hard_blocks_are_blocked_in_both_modes(name, request_, reason, mode) -> None:
    d = decide(request_, PolicyConfig(), mode=mode, kill_switch=False)
    assert d.outcome is OUT.BLOCKED and reason in d.reasons


def test_a_hard_block_beats_every_autonomy_level() -> None:
    all_auto = {k: AutonomyLevel.AUTO for k in DEFAULT_LEVELS}
    d = decide(email(injection_flagged=True), PolicyConfig(levels=all_auto), mode=A, kill_switch=False)
    assert d.outcome is OUT.BLOCKED


def test_missing_discount_limit_blocks_any_discount() -> None:
    config = PolicyConfig(max_discount_pct=None)
    d = decide(req("send_io", io_validated=True, discount_pct=1), config, mode=A, kill_switch=False)
    assert d.outcome is OUT.BLOCKED


# Kill switch ---------------------------------------------------------------------------------

@pytest.mark.parametrize("mode", [A, M])
def test_kill_switch_holds_every_outbound_action(mode) -> None:
    for r in (email(), email(kind="follow_up"), req("send_io", io_validated=True)):
        assert decide(r, PolicyConfig(), mode=mode, kill_switch=True).held is True
        assert decide(r, PolicyConfig(), mode=mode, kill_switch=False).held is False


def test_kill_switch_does_not_hold_internal_actions() -> None:
    assert decide(req("score"), PolicyConfig(), mode=A, kill_switch=True).held is False


# Mode flip and pack-driven levels -----------------------------------------------------------

def test_flipping_the_mode_changes_outcomes_with_no_code_change() -> None:
    r = email(tier=Tier.A)
    config = PolicyConfig()
    assert decide(r, config, mode=A, kill_switch=False).outcome is OUT.AUTO_WITH_VETO
    assert decide(r, config, mode=M, kill_switch=False).outcome is OUT.NEEDS_APPROVAL
    assert decide(r, config, mode=A, kill_switch=False).outcome is OUT.AUTO_WITH_VETO


def test_levels_are_plain_data_a_pack_can_change() -> None:
    levels = {**DEFAULT_LEVELS, "first_touch.B": AutonomyLevel.NEEDS_APPROVAL}
    d = decide(email(tier=Tier.B), PolicyConfig(levels=levels), mode=A, kill_switch=False)
    assert d.outcome is OUT.NEEDS_APPROVAL


def test_an_action_removed_from_the_data_is_denied() -> None:
    levels = {k: v for k, v in DEFAULT_LEVELS.items() if k != "generate_io"}
    d = decide(req("generate_io"), PolicyConfig(levels=levels), mode=A, kill_switch=False)
    assert d.outcome is OUT.DENIED


@pytest.mark.parametrize("text,expected", [
    ("What is your pricing?", True), ("Can you give a discount", True),
    ("our CPM is high", True), ("ROAS $15.52 vs $3.80", False), ("Happy to chat Tuesday", False),
])
def test_price_detection(text, expected) -> None:
    assert mentions_price(text) is expected
