-- Step 8A: case-study bookkeeping and the kill-switch resume epoch.

-- A case study in the database is keyed by the pack's id (slug), so a pack can be synced again.
ALTER TABLE case_studies ADD COLUMN slug text;
CREATE UNIQUE INDEX case_studies_slug_key ON case_studies (segment_id, slug);

-- Which case study went to which deal, once per sent email.
ALTER TABLE case_study_sends ADD COLUMN action_id uuid REFERENCES actions(id);
CREATE UNIQUE INDEX case_study_sends_action_key ON case_study_sends (action_id);

-- Each time the kill switch is turned off the epoch goes up. Resume events are keyed by
-- (action, epoch), so releasing twice in one epoch cannot queue a second send.
INSERT INTO settings (key, value) VALUES ('kill_switch_epoch', '0') ON CONFLICT DO NOTHING;
