-- The runtime switches (kill switch, autonomy mode, veto window) live in system_settings. A value here
-- overrides the environment; every change is audited with its actor, and updated_by shows who last set it.
ALTER TABLE settings RENAME TO system_settings;
ALTER TABLE system_settings ADD COLUMN IF NOT EXISTS updated_by text;
