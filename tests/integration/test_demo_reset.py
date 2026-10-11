import random
from datetime import UTC, datetime

from revos.demo_reset import cancel_pending_work
from revos.runtime.worker import Worker
from tests.pipeline_helpers import audit, make_rig

SUNDAY = datetime(2026, 10, 11, 9, 0, tzinfo=UTC)


def test_a_held_email_cannot_send_after_the_local_reset_even_when_its_timer_fires(conn, ro_conn, pack, clock):
    clock.set(SUNDAY)
    rig = make_rig(conn, ro_conn, pack, clock)
    first = rig.orch.handle_lead_created(rig.deal_id)
    assert rig.orch.send_action(first.action_id).status == "deferred"          # held until Monday
    counts = cancel_pending_work(conn)
    assert counts["actions"] >= 1 and counts["jobs"] >= 1
    assert conn.execute("SELECT status FROM actions WHERE id = %s", (first.action_id,)).fetchone()[0] \
        == "cancelled"
    assert audit(conn, "demo.reset", rig.deal_id)
    clock.advance(2 * 86400)                                                   # Monday has come
    worker = Worker(rig.orch.handlers(), clock, rng=random.Random(1))
    while worker.run_once(conn):
        pass
    assert rig.world.mailer.sent == [] if hasattr(rig.world, "mailer") else True
    assert conn.execute("SELECT count(*) FROM messages WHERE direction = 'outbound'").fetchone()[0] == 0
    assert conn.execute("SELECT status FROM actions WHERE id = %s", (first.action_id,)).fetchone()[0] \
        == "cancelled"
    assert cancel_pending_work(conn) == {"actions": 0, "jobs": 0}              # safe to repeat
