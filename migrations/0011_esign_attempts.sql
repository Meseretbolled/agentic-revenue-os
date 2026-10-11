-- One row per IO sent for signature: what we know about the Dropbox Sign request. It is written on its own
-- connection BEFORE the create call, so a crash or a lost response is never mistaken for "not sent".
CREATE TABLE IF NOT EXISTS esign_attempts (
    idempotency_key      text PRIMARY KEY,
    state                text NOT NULL CHECK (state IN ('sending', 'created', 'review')),
    signature_request_id text,
    created_at           timestamptz NOT NULL DEFAULT now(),
    updated_at           timestamptz NOT NULL DEFAULT now()
);
