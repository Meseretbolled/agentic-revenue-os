-- Slack: which message shows which action (so it can be updated in place), and which button presses
-- were already handled (so a replayed payload does nothing).
CREATE TABLE IF NOT EXISTS slack_messages (
    id         bigserial PRIMARY KEY,
    kind       text NOT NULL,                    -- 'action' | 'digest'
    ref        text NOT NULL,                    -- the action id, or the day for a digest
    channel    text NOT NULL,
    ts         text NOT NULL,                    -- Slack's message id
    state      text NOT NULL DEFAULT '',         -- what the message last said (pending, sent, ...)
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (kind, ref)
);

CREATE TABLE IF NOT EXISTS slack_interactions (
    key        text PRIMARY KEY,                 -- trigger id + action id: one press, handled once
    user_id    text NOT NULL,
    action_id  uuid,
    verb       text NOT NULL,
    outcome    text NOT NULL,
    at         timestamptz NOT NULL DEFAULT now()
);
