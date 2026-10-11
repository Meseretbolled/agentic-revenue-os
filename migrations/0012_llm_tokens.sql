-- Tokens per model call, next to its cost (input includes cache reads and writes).
ALTER TABLE llm_spend ADD COLUMN IF NOT EXISTS input_tokens integer;
ALTER TABLE llm_spend ADD COLUMN IF NOT EXISTS output_tokens integer;
