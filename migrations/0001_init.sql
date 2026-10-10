-- Core schema. Plain SQL, applied in order by revos.db.migrate().
-- Enums are text + CHECK so a new value is a migration, not a type rewrite.

CREATE TABLE segments (
    id           text PRIMARY KEY,
    pack_version text NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE companies (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    segment_id  text NOT NULL REFERENCES segments(id),
    name        text NOT NULL,
    domain      text,
    data        jsonb NOT NULL DEFAULT '{}',
    hubspot_id  text,
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE contacts (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id        uuid REFERENCES companies(id),
    email             text NOT NULL,
    full_name         text,
    timezone          text,
    suppressed        boolean NOT NULL DEFAULT false,
    suppressed_reason text,
    suppressed_at     timestamptz,
    hubspot_id        text,
    created_at        timestamptz NOT NULL DEFAULT now(),
    CHECK (NOT suppressed OR suppressed_at IS NOT NULL)
);
CREATE UNIQUE INDEX contacts_email_key ON contacts (lower(email));

CREATE TABLE deals (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    segment_id       text NOT NULL REFERENCES segments(id),
    company_id       uuid REFERENCES companies(id),
    contact_id       uuid REFERENCES contacts(id),
    stage            text NOT NULL DEFAULT 'new' CHECK (stage IN (
        'new','enriched','scored','outreach','replied','meeting','qualified',
        'proposal','io_approval','out_for_signature','closed_won','closed_lost',
        'handoff','do_not_contact')),
    version          integer NOT NULL DEFAULT 1,
    tier             text CHECK (tier IN ('A','B','C','disqualified')),
    fit_score        integer,
    intent_score     integer,
    engagement_score integer,
    total_score      integer,
    score_reason     text,
    hubspot_deal_id  text,
    fields           jsonb NOT NULL DEFAULT '{}',   -- call/qualification fields
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now()
);

-- Rule: a deal's stage changes only through transition_deal(), which sets
-- revos.allow_stage_change for its own transaction. Anything else is refused.
CREATE FUNCTION guard_deal_stage() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NEW.stage <> 'new' THEN
            RAISE EXCEPTION 'deals must be created in stage new, not %', NEW.stage;
        END IF;
    ELSIF NEW.stage IS DISTINCT FROM OLD.stage
          AND coalesce(current_setting('revos.allow_stage_change', true), 'off') <> 'on' THEN
        RAISE EXCEPTION 'deal stage may only change through transition_deal()';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER deals_stage_guard BEFORE INSERT OR UPDATE ON deals
    FOR EACH ROW EXECUTE FUNCTION guard_deal_stage();

CREATE TABLE events (
    id              bigserial PRIMARY KEY,
    idempotency_key text NOT NULL UNIQUE,
    type            text NOT NULL,
    payload         jsonb NOT NULL DEFAULT '{}',
    received_at     timestamptz NOT NULL DEFAULT now(),
    processed_at    timestamptz
);

CREATE TABLE jobs (
    id           bigserial PRIMARY KEY,
    event_id     bigint REFERENCES events(id),
    type         text NOT NULL,
    payload      jsonb NOT NULL DEFAULT '{}',
    status       text NOT NULL DEFAULT 'queued'
                 CHECK (status IN ('queued','running','done','failed','dead')),
    run_at       timestamptz NOT NULL DEFAULT now(),
    attempts     integer NOT NULL DEFAULT 0,
    max_attempts integer NOT NULL DEFAULT 5,
    lease_until  timestamptz,
    last_error   text,
    created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX jobs_claim_idx ON jobs (run_at) WHERE status = 'queued';

CREATE TABLE dead_letters (
    id         bigserial PRIMARY KEY,
    job_id     bigint REFERENCES jobs(id),
    type       text NOT NULL,
    payload    jsonb NOT NULL DEFAULT '{}',
    error      text,
    created_at timestamptz NOT NULL DEFAULT now(),
    retried_at timestamptz
);

-- Outbox: every side effect is an action row with a unique idempotency key.
CREATE TABLE actions (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    idempotency_key text NOT NULL UNIQUE,
    deal_id         uuid REFERENCES deals(id),
    contact_id      uuid REFERENCES contacts(id),
    type            text NOT NULL,
    risk            text NOT NULL DEFAULT 'low' CHECK (risk IN ('low','medium','high')),
    autonomy_level  text NOT NULL CHECK (autonomy_level IN ('auto','auto_with_veto','needs_approval')),
    status          text NOT NULL DEFAULT 'pending' CHECK (status IN (
        'pending','awaiting_approval','approved','cancelled','sent','failed')),
    decision        text CHECK (decision IN ('approved','vetoed','rejected')),
    decision_by     text,          -- 'policy' for auto actions, otherwise a person
    decided_at      timestamptz,
    provider_id     text,          -- message id, CRM record id, envelope id
    payload         jsonb NOT NULL DEFAULT '{}',
    run_at          timestamptz NOT NULL DEFAULT now(),
    created_at      timestamptz NOT NULL DEFAULT now(),
    -- Rule 5: nothing is sent without a recorded approving decision.
    CONSTRAINT sent_requires_decision CHECK (
        status <> 'sent' OR (decision = 'approved' AND decision_by IS NOT NULL
                             AND decided_at IS NOT NULL)),
    -- Fail closed: an email action must name its contact so suppression can be checked.
    CONSTRAINT email_requires_contact CHECK (type <> 'send_email' OR contact_id IS NOT NULL)
);

-- Suppression is enforced in the database, not only in policy code.
CREATE FUNCTION guard_suppressed_send() RETURNS trigger AS $$
BEGIN
    IF NEW.type = 'send_email'
       AND NEW.status IN ('pending','awaiting_approval','approved','sent')
       AND EXISTS (SELECT 1 FROM contacts WHERE id = NEW.contact_id AND suppressed) THEN
        RAISE EXCEPTION 'contact % is suppressed: email may not be queued or sent', NEW.contact_id;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER actions_suppression_guard BEFORE INSERT OR UPDATE ON actions
    FOR EACH ROW EXECUTE FUNCTION guard_suppressed_send();

CREATE TABLE approvals (
    id         bigserial PRIMARY KEY,
    action_id  uuid NOT NULL REFERENCES actions(id),
    decision   text NOT NULL CHECK (decision IN ('approved','vetoed','rejected')),
    decided_by text NOT NULL,
    reason     text,
    decided_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE messages (
    id                bigserial PRIMARY KEY,
    deal_id           uuid REFERENCES deals(id),
    contact_id        uuid REFERENCES contacts(id),
    direction         text NOT NULL CHECK (direction IN ('outbound','inbound')),
    message_id        text,
    in_reply_to       text,
    thread_id         text,
    subject           text,
    body              text,
    quarantined       boolean NOT NULL DEFAULT false,
    classification    text,
    created_at        timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX messages_message_id_key ON messages (message_id) WHERE message_id IS NOT NULL;

CREATE TABLE transcripts (
    id          bigserial PRIMARY KEY,
    deal_id     uuid REFERENCES deals(id),
    external_id text UNIQUE,
    body        text NOT NULL,
    extracted   jsonb,
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE lessons (
    id          bigserial PRIMARY KEY,
    segment_id  text REFERENCES segments(id),   -- NULL = global lesson
    text        text NOT NULL,
    evidence    text NOT NULL,                  -- the quote that supports it
    tags        text[] NOT NULL DEFAULT '{}',
    confidence  numeric(3,2) NOT NULL DEFAULT 0.5 CHECK (confidence BETWEEN 0 AND 1),
    uses        integer NOT NULL DEFAULT 0,
    retired_at  timestamptz,
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE lesson_uses (
    id         bigserial PRIMARY KEY,
    lesson_id  bigint NOT NULL REFERENCES lessons(id),
    deal_id    uuid REFERENCES deals(id),
    action_id  uuid REFERENCES actions(id),
    used_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE case_studies (
    id         bigserial PRIMARY KEY,
    segment_id text REFERENCES segments(id),
    title      text NOT NULL,
    tags       text[] NOT NULL DEFAULT '{}',
    metric     text,
    source_url text NOT NULL,                   -- honesty rule: always link the source
    label      text NOT NULL DEFAULT 'public testimonial' CHECK (label = 'public testimonial')
);

CREATE TABLE case_study_sends (
    id            bigserial PRIMARY KEY,
    case_study_id bigint NOT NULL REFERENCES case_studies(id),
    deal_id       uuid NOT NULL REFERENCES deals(id),
    message_id    bigint REFERENCES messages(id),
    sent_at       timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE documents (
    id         bigserial PRIMARY KEY,
    deal_id    uuid NOT NULL REFERENCES deals(id),
    kind       text NOT NULL,                   -- e.g. io
    version    integer NOT NULL DEFAULT 1,
    body       text,
    validation jsonb,
    provider_id text,                           -- e-sign envelope id
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (deal_id, kind, version)
);

CREATE TABLE settings (
    key        text PRIMARY KEY,
    value      jsonb NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);
INSERT INTO settings (key, value) VALUES
    ('kill_switch', 'false'),
    ('autonomy_mode', '"autonomous"');

-- Append-only audit log. txid lets a test prove the row shares the change's transaction.
CREATE TABLE audit_log (
    id        bigserial PRIMARY KEY,
    at        timestamptz NOT NULL DEFAULT now(),
    txid      bigint NOT NULL DEFAULT txid_current(),
    actor     text NOT NULL,
    event     text NOT NULL,
    deal_id   uuid,
    detail    jsonb NOT NULL DEFAULT '{}'
);
CREATE INDEX audit_log_deal_idx ON audit_log (deal_id, id);

CREATE FUNCTION audit_log_append_only() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'audit_log is append-only';
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER audit_log_no_change BEFORE UPDATE OR DELETE ON audit_log
    FOR EACH ROW EXECUTE FUNCTION audit_log_append_only();
