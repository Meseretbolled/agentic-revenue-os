-- One correlation id follows an event through the queue, the agents, the policy engine and the
-- outbox. The worker sets the session setting revos.correlation_id for the job it runs, and these
-- columns default to it, so rows written while handling a job carry it without every INSERT
-- naming it.
ALTER TABLE events      ADD COLUMN IF NOT EXISTS correlation_id text;
ALTER TABLE jobs        ADD COLUMN IF NOT EXISTS correlation_id text;
ALTER TABLE actions     ADD COLUMN IF NOT EXISTS correlation_id text
    DEFAULT NULLIF(current_setting('revos.correlation_id', true), '');
ALTER TABLE agent_runs  ADD COLUMN IF NOT EXISTS correlation_id text
    DEFAULT NULLIF(current_setting('revos.correlation_id', true), '');
ALTER TABLE audit_log   ADD COLUMN IF NOT EXISTS correlation_id text
    DEFAULT NULLIF(current_setting('revos.correlation_id', true), '');
CREATE INDEX IF NOT EXISTS audit_log_correlation_idx ON audit_log (correlation_id);
CREATE INDEX IF NOT EXISTS events_correlation_idx ON events (correlation_id);
