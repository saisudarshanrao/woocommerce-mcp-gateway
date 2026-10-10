-- How many tool calls each API key (or, for the chat, each user) has made in
-- the current minute. It lives in the database rather than in memory so that
-- the limit holds across gateway replicas. A subject has one row at a time:
-- the row for an earlier minute is removed when a new minute's row is written.
CREATE TABLE rate_counters (
    subject text        NOT NULL,  -- "key:<id>" or "user:<id>"
    minute  timestamptz NOT NULL,
    calls   integer     NOT NULL,
    PRIMARY KEY (subject, minute)
);

-- A call refused for being over the limit is audited with its own outcome.
ALTER TABLE audit_log DROP CONSTRAINT audit_log_outcome_check;
ALTER TABLE audit_log ADD CONSTRAINT audit_log_outcome_check
    CHECK (outcome IN ('ok', 'error', 'timeout', 'unreachable', 'denied', 'limited'));
