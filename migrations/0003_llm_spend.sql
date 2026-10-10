-- LLM spend ledger. A call reserves its worst-case cost before it runs, under one lock, so two
-- concurrent calls cannot both squeeze under the cap. Then the real cost replaces the reserve.
CREATE TABLE llm_spend (
    id           bigserial PRIMARY KEY,
    at           timestamptz NOT NULL DEFAULT now(),
    task         text NOT NULL,
    model        text NOT NULL,
    reserved_usd numeric(10,4) NOT NULL CHECK (reserved_usd >= 0),
    cost_usd     numeric(10,4) CHECK (cost_usd >= 0),
    status       text NOT NULL DEFAULT 'reserved' CHECK (status IN ('reserved','settled','failed'))
);
