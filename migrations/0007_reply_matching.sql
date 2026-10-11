-- Reply matching: the Message-ID Gmail really assigned to an email we sent, and replies we could
-- not safely attach to one deal.
ALTER TABLE messages ADD COLUMN IF NOT EXISTS real_message_id text;
CREATE INDEX IF NOT EXISTS messages_real_message_id_idx ON messages (real_message_id)
    WHERE real_message_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS held_replies (
    id            bigserial PRIMARY KEY,
    message_id    text NOT NULL UNIQUE,
    from_addr     text NOT NULL,
    subject       text,
    body          text,                       -- untrusted data; shown only as data
    received_at   timestamptz,
    reason        text NOT NULL CHECK (reason IN ('ambiguous')),
    candidate_deal_ids uuid[] NOT NULL DEFAULT '{}',
    created_at    timestamptz NOT NULL DEFAULT now(),
    resolved_at   timestamptz
);
