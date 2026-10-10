-- Step 7: agent runs, their steps and their proposals. Every step is recorded as it happens, so a
-- crashed run resumes from the database and a recorded success is never run again.

CREATE TABLE agent_runs (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    idempotency_key text NOT NULL UNIQUE,            -- run:<deal id>:<step>
    deal_id         uuid NOT NULL REFERENCES deals(id),
    agent           text NOT NULL,
    step            text NOT NULL,
    runner          text NOT NULL CHECK (runner IN ('rules', 'sdk')),
    status          text NOT NULL DEFAULT 'running'
                    CHECK (status IN ('running', 'completed', 'needs_human', 'failed')),
    input           jsonb NOT NULL DEFAULT '{}',
    output          jsonb,
    human_reason    text,
    step_count      integer NOT NULL DEFAULT 0,
    started_at      timestamptz NOT NULL DEFAULT now(),
    finished_at     timestamptz,
    CHECK (status <> 'needs_human' OR human_reason IS NOT NULL)
);

CREATE TABLE agent_steps (
    run_id    uuid NOT NULL REFERENCES agent_runs(id),
    idx       integer NOT NULL,
    kind      text NOT NULL CHECK (kind IN ('tool_call', 'llm_call')),
    name      text NOT NULL,
    args_hash text NOT NULL,
    result    jsonb,
    ok        boolean NOT NULL,
    at        timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, idx)
);

CREATE TABLE agent_proposals (
    run_id       uuid NOT NULL REFERENCES agent_runs(id),
    idx          integer NOT NULL,
    action_type  text NOT NULL,
    payload      jsonb NOT NULL DEFAULT '{}',
    evidence_ids text[] NOT NULL DEFAULT '{}',
    reason       text NOT NULL,
    status       text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'applied', 'rejected')),
    action_id    uuid REFERENCES actions(id),
    outcome      text,
    PRIMARY KEY (run_id, idx)
);
