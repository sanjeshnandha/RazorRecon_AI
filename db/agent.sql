-- ---------------------------------------------------------------------------
-- Investigation agent: transcript storage.
--
-- Applied SEPARATELY from schema.sql and written to be idempotent, because
-- schema.sql is destructive -- it drops every table. Adding an optional feature
-- must never cost anyone their dataset and their run history, so this file can
-- be run against a live database as many times as you like:
--
--     psql "$DATABASE_URL" -f db/agent.sql
--     make agent-schema          # the same thing
--
-- The API also applies it on demand, so an existing install picks the table up
-- on its own. schema.sql still creates it too, so a fresh load needs nothing
-- extra.
--
-- The agent explains results; it never writes to any reconciliation table. This
-- is its own record: what was asked, what it answered, which tools it called,
-- and whether every id it cited actually appeared in the evidence it read.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS agent_transcripts (
    run_id                 UUID        NOT NULL REFERENCES reconciliation_runs(run_id)
                                       ON DELETE CASCADE,
    turn_id                BIGINT      NOT NULL,
    asked_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
    question               TEXT        NOT NULL,
    answer                 TEXT        NOT NULL,
    provider               TEXT,
    model                  TEXT,
    tool_calls             JSONB       NOT NULL DEFAULT '[]',
    tool_call_count        INT         NOT NULL DEFAULT 0,
    citations              TEXT[]      NOT NULL DEFAULT '{}',
    unsupported_references TEXT[]      NOT NULL DEFAULT '{}',
    grounded               BOOLEAN     NOT NULL DEFAULT TRUE,
    stop_reason            TEXT,
    elapsed_seconds        NUMERIC(8,2),
    PRIMARY KEY (run_id, turn_id)
);
CREATE INDEX IF NOT EXISTS agent_transcript_run_idx ON agent_transcripts (run_id, turn_id);
