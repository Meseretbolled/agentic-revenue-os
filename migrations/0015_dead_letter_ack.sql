-- A person can acknowledge a dead letter: it leaves the main count but stays viewable. Who and when are kept.
ALTER TABLE dead_letters ADD COLUMN IF NOT EXISTS acknowledged_at timestamptz;
ALTER TABLE dead_letters ADD COLUMN IF NOT EXISTS acknowledged_by text;
