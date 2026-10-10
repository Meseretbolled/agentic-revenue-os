"""Loop limits and record/replay of tool calls."""

import pytest

from revos.agents.broker import Limits, ToolBroker
from revos.agents.store import RunStore, canonical_hash
from revos.domain.errors import LoopLimitExceeded, ToolCallBlocked
from tests.mcp_helpers import make_ctx, make_deal


@pytest.fixture
def setup(conn, ro_conn, pack):
    deal = make_deal(conn, "brightside-optical.example", "sam@brightside-optical.example")
    other = make_deal(conn, "larkspur-autos.example", "pat@larkspur-autos.example")
    store = RunStore(conn)
    run = store.start(deal["deal"], "outreach", "t", "rules", {})
    ctx, _ = make_ctx(ro_conn, pack, deal["deal"])

    def broker(limits=None, run_id=None):
        return ToolBroker(store, run_id or run.id, "outreach", ctx, limits)

    return deal, other, store, run, broker


def search(broker, query):
    return broker.call("lessons.search", {"query": query, "limit": 1})


def test_the_canonical_hash_ignores_key_order_and_spacing() -> None:
    assert canonical_hash({"a": 1, "b": [1, 2]}) == canonical_hash({"b": [1, 2], "a": 1})
    assert canonical_hash({"a": 1}) != canonical_hash({"a": 2})


def test_a_repeated_identical_call_is_blocked_on_the_third_try(setup) -> None:
    _, _, _, _, broker = setup
    b = broker()
    search(b, "x")
    search(b, "x")
    with pytest.raises(LoopLimitExceeded, match="repeated identical call"):
        search(b, "x")
    assert b.tripped and b.executed == 2


def test_the_same_call_with_arguments_in_another_order_still_counts_as_identical(setup) -> None:
    _, _, _, _, broker = setup
    b = broker()
    b.call("lessons.search", {"query": "x", "limit": 1, "tags": []})
    b.call("lessons.search", {"tags": [], "limit": 1, "query": "x"})
    with pytest.raises(LoopLimitExceeded):
        b.call("lessons.search", {"limit": 1, "query": "x", "tags": []})


def test_failed_calls_count_toward_the_repeat_limit_too(setup) -> None:
    _, other, _, _, broker = setup
    b = broker()
    foreign = {"deal_id": str(other["deal"])}
    for _ in range(2):
        with pytest.raises(ToolCallBlocked):
            b.call("crm.get_deal", foreign)
    with pytest.raises(LoopLimitExceeded):
        b.call("crm.get_deal", foreign)


def test_errors_in_a_row_stop_the_run(setup) -> None:
    deal, other, store, run, broker = setup
    b = broker()
    import uuid
    for _ in range(2):
        with pytest.raises(ToolCallBlocked):
            b.call("crm.get_deal", {"deal_id": str(uuid.uuid4())})
    assert b.tripped is None
    with pytest.raises(LoopLimitExceeded, match="3 tool errors in a row"):
        b.call("crm.get_deal", {"deal_id": str(uuid.uuid4())})
    assert b.tripped == "3 tool errors in a row"
    with pytest.raises(LoopLimitExceeded):
        search(b, "anything")                            # once tripped, nothing else runs


def test_a_success_resets_the_error_streak(setup) -> None:
    import uuid
    _, _, _, _, broker = setup
    b = broker()
    for _i in range(2):
        with pytest.raises(ToolCallBlocked):
            b.call("crm.get_deal", {"deal_id": str(uuid.uuid4())})
    search(b, "ok")
    for _i in range(2):
        with pytest.raises(ToolCallBlocked):
            b.call("crm.get_deal", {"deal_id": str(uuid.uuid4())})
    assert b.tripped is None


def test_the_total_number_of_calls_is_capped(setup) -> None:
    _, _, _, _, broker = setup
    b = broker(Limits(max_calls=3))
    for i in range(3):
        search(b, f"q{i}")
    with pytest.raises(LoopLimitExceeded, match="more than 3 tool calls"):
        search(b, "q3")


def test_the_cap_counts_calls_from_before_a_restart_too(setup) -> None:
    _, _, store, run, broker = setup
    first = broker(Limits(max_calls=3))
    for i in range(3):
        search(first, f"q{i}")
    resumed = broker(Limits(max_calls=3))                 # a new process, same run
    for i in range(3):
        search(resumed, f"q{i}")                         # replayed from the record: free
    assert resumed.replayed == 3 and resumed.executed == 0
    with pytest.raises(LoopLimitExceeded):
        search(resumed, "new")


def test_a_recorded_success_is_replayed_and_not_executed_again(setup) -> None:
    _, _, store, run, broker = setup
    first = broker()
    a = search(first, "x")
    second = broker()
    assert search(second, "x") == a
    assert (second.executed, second.replayed) == (0, 1)
    assert len(store.steps(run.id)) == 1                  # the replay wrote nothing new


def test_a_recorded_failure_is_run_again_not_replayed(setup) -> None:
    import uuid
    _, _, store, run, broker = setup
    bad = {"deal_id": str(uuid.uuid4())}
    first = broker()
    with pytest.raises(ToolCallBlocked):
        first.call("crm.get_deal", bad)
    second = broker()
    with pytest.raises(ToolCallBlocked):
        second.call("crm.get_deal", bad)
    assert (second.executed, second.replayed) == (1, 0)
    assert [s["ok"] for s in store.steps(run.id)] == [False, False]


def test_steps_are_numbered_in_order_and_a_finished_run_accepts_no_more(setup) -> None:
    from revos.domain.errors import RevosError
    deal, _, store, run, broker = setup
    b = broker()
    search(b, "a")
    search(b, "b")
    assert [s["idx"] for s in store.steps(run.id)] == [1, 2]
    store.finish(run.id, deal["deal"], "completed", {}, None, ())
    with pytest.raises(RevosError, match="not running"):
        search(broker(), "c")


def test_a_finished_run_is_not_finished_twice(setup) -> None:
    deal, _, store, run, _ = setup
    assert store.finish(run.id, deal["deal"], "completed", {"a": 1}, None, ()) is True
    assert store.finish(run.id, deal["deal"], "needs_human", {"a": 2}, "late", ()) is False
