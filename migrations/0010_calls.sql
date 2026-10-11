-- Calls (Fireflies transcripts) that could not be tied to exactly one deal are held for a person.
-- Only what the title and time say is kept here; the transcript text is not stored.
CREATE TABLE IF NOT EXISTS held_calls (
    id                 bigserial PRIMARY KEY,
    external_id        text NOT NULL UNIQUE,
    title              text,
    started_at         timestamptz,
    reason             text NOT NULL CHECK (reason IN ('ambiguous', 'none')),
    candidate_deal_ids uuid[] NOT NULL DEFAULT '{}',
    created_at         timestamptz NOT NULL DEFAULT now(),
    resolved_at        timestamptz
);
