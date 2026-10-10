-- Step 8B: where a lesson came from, how it has fared, and why it was retired.

ALTER TABLE lessons
    ADD COLUMN source         text NOT NULL DEFAULT 'call' CHECK (source IN ('call', 'reply', 'manual')),
    ADD COLUMN wins           integer NOT NULL DEFAULT 0,      -- uses followed by a reply or meeting
    ADD COLUMN losses         integer NOT NULL DEFAULT 0,      -- uses followed by a bad reply or silence
    ADD COLUMN retired_reason text,
    ADD CONSTRAINT lessons_retired_has_reason CHECK (retired_at IS NULL OR retired_reason IS NOT NULL);

-- One row per (lesson, action) it shaped. The outcome moves forward once and is never rewritten.
ALTER TABLE lesson_uses
    ADD COLUMN outcome    text NOT NULL DEFAULT 'pending' CHECK (outcome IN
        ('pending', 'reply_positive', 'meeting', 'reply_negative', 'no_reply')),
    ADD COLUMN outcome_at timestamptz;
CREATE UNIQUE INDEX lesson_uses_action_key ON lesson_uses (lesson_id, action_id)
    WHERE action_id IS NOT NULL;
