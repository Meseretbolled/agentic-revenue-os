import psycopg
import pytest


def make_contact(conn, suppressed: bool = False):
    return conn.execute(
        "INSERT INTO contacts (email, suppressed, suppressed_at) "
        "VALUES (%s, %s, CASE WHEN %s THEN now() END) RETURNING id",
        (f"p{suppressed}@example.test", suppressed, suppressed),
    ).fetchone()[0]


def insert_action(conn, key: str, contact, **cols):
    base = {"type": "send_email", "autonomy_level": "auto", "status": "pending"}
    base.update(cols)
    names = ", ".join(["idempotency_key", "contact_id", *base])
    marks = ", ".join(["%s"] * (2 + len(base)))
    return conn.execute(
        f"INSERT INTO actions ({names}) VALUES ({marks}) RETURNING id", (key, contact, *base.values())
    ).fetchone()[0]


def test_sent_without_a_decision_is_refused(conn) -> None:
    contact = make_contact(conn)
    with pytest.raises(psycopg.errors.CheckViolation, match="sent_requires_decision"):
        insert_action(conn, "k1", contact, status="sent")


def test_sent_with_decision_but_no_approver_is_refused(conn) -> None:
    contact = make_contact(conn)
    with pytest.raises(psycopg.errors.CheckViolation, match="sent_requires_decision"):
        insert_action(conn, "k2", contact, status="sent", decision="approved",
                      decided_at="2026-01-01")


def test_pending_action_cannot_be_flipped_to_sent_without_approver(conn) -> None:
    contact = make_contact(conn)
    action = insert_action(conn, "k3", contact)
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute("UPDATE actions SET status = 'sent' WHERE id = %s", (action,))


def test_sent_with_a_recorded_approval_is_accepted(conn) -> None:
    contact = make_contact(conn)
    insert_action(conn, "k4", contact, status="sent", decision="approved",
                  decision_by="policy", decided_at="2026-01-01")


def test_send_to_suppressed_contact_is_refused(conn) -> None:
    contact = make_contact(conn, suppressed=True)
    with pytest.raises(psycopg.errors.RaiseException, match="suppressed"):
        insert_action(conn, "k5", contact)


def test_contact_suppressed_after_queueing_cannot_be_sent(conn) -> None:
    contact = make_contact(conn)
    action = insert_action(conn, "k6", contact)
    conn.execute("UPDATE contacts SET suppressed = true, suppressed_at = now() WHERE id = %s", (contact,))
    with pytest.raises(psycopg.errors.RaiseException, match="suppressed"):
        conn.execute(
            "UPDATE actions SET status = 'sent', decision = 'approved', decision_by = 'policy', "
            "decided_at = now() WHERE id = %s", (action,))
    # cancelling is still allowed
    conn.execute("UPDATE actions SET status = 'cancelled' WHERE id = %s", (action,))


def test_email_action_without_contact_is_refused(conn) -> None:
    with pytest.raises(psycopg.errors.CheckViolation, match="email_requires_contact"):
        insert_action(conn, "k7", None)


def test_idempotency_key_is_unique(conn) -> None:
    contact = make_contact(conn)
    insert_action(conn, "dup", contact)
    with pytest.raises(psycopg.errors.UniqueViolation):
        insert_action(conn, "dup", contact)
