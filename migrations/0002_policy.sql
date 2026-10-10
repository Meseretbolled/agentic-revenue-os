-- Step 3: policy engine support. Hold/veto bookkeeping, draft status, send counters.

ALTER TABLE actions
    ADD COLUMN hold_reason text,           -- why an approved action has not gone out yet
    ADD COLUMN veto_until  timestamptz,    -- end of the veto window for auto_with_veto
    ADD COLUMN sent_at     timestamptz;

ALTER TABLE actions DROP CONSTRAINT actions_status_check;
ALTER TABLE actions ADD CONSTRAINT actions_status_check CHECK (status IN (
    'pending','awaiting_approval','approved','cancelled','sent','failed','draft'));

-- Outbound action types that reach a person. Keep in sync with revos.policy.config.OUTBOUND_TYPES
-- (a test checks every one of them against these guards).
ALTER TABLE actions DROP CONSTRAINT email_requires_contact;
ALTER TABLE actions ADD CONSTRAINT email_requires_contact CHECK (
    type NOT IN ('send_email','first_touch','follow_up','send_io') OR contact_id IS NOT NULL);

CREATE OR REPLACE FUNCTION guard_suppressed_send() RETURNS trigger AS $$
BEGIN
    IF NEW.type IN ('send_email','first_touch','follow_up','send_io')
       AND NEW.status IN ('pending','awaiting_approval','approved','sent')
       AND EXISTS (SELECT 1 FROM contacts WHERE id = NEW.contact_id AND suppressed) THEN
        RAISE EXCEPTION 'contact % is suppressed: email may not be queued or sent', NEW.contact_id;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- Atomic send counters. scope is 'daily' or 'domain:<name>'; one row per scope per day.
CREATE TABLE send_counters (
    scope text NOT NULL,
    day   date NOT NULL,
    count integer NOT NULL DEFAULT 0 CHECK (count >= 0),
    PRIMARY KEY (scope, day)
);
