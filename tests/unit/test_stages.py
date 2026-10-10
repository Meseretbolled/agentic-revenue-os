import pytest

from revos.domain.stages import ALLOWED_TRANSITIONS, TERMINAL, DealStage, can_transition


def test_happy_path_is_allowed_step_by_step() -> None:
    path = ["new", "enriched", "scored", "outreach", "replied", "meeting", "qualified",
            "proposal", "io_approval", "out_for_signature", "closed_won", "handoff"]
    for src, dst in zip(path, path[1:], strict=False):
        assert can_transition(DealStage(src), DealStage(dst))


@pytest.mark.parametrize("src,dst", [
    ("new", "qualified"), ("scored", "closed_won"), ("outreach", "meeting"),
    ("closed_won", "outreach"), ("handoff", "new"),
])
def test_skips_and_backwards_moves_are_refused(src: str, dst: str) -> None:
    assert not can_transition(DealStage(src), DealStage(dst))


def test_do_not_contact_reachable_from_every_live_stage() -> None:
    live = set(DealStage) - TERMINAL - {DealStage.CLOSED_WON, DealStage.CLOSED_LOST}
    assert all(can_transition(s, DealStage.DO_NOT_CONTACT) for s in live)


def test_terminal_stages_have_no_exit() -> None:
    assert all(not ALLOWED_TRANSITIONS[s] for s in TERMINAL)
