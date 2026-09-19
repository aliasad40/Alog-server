-- 001 -- Pin the timestamp columns to UTC.
--
-- Why this is safe on a populated table
-- -------------------------------------
-- ClickHouse stores DateTime as epoch seconds. A column's timezone is metadata
-- used only when parsing input and formatting output; it is NOT part of the
-- stored value. So this ALTER rewrites nothing and touches no data parts --
-- it is a metadata-only change that completes instantly regardless of table
-- size. Existing rows keep the exact instants they already had.
--
-- What it changes going forward: a naive datetime sent on INSERT used to be
-- interpreted in the ClickHouse server's own timezone. Now it is interpreted
-- as UTC, which removes a dependency on a setting the application does not
-- control. The worker sends UTC-aware datetimes as of v1.1.0, so the two
-- agree either way.
--
-- Display conversion to the operator's timezone happens in the application
-- (app/timezones.py), driven by the display_timezone setting.

ALTER TABLE network_logs.nat_logs
    MODIFY COLUMN IF EXISTS timestamp DateTime('UTC') CODEC(DoubleDelta, ZSTD(1));

ALTER TABLE network_logs.nat_logs
    MODIFY COLUMN IF EXISTS session_end_time Nullable(DateTime('UTC')) CODEC(DoubleDelta, ZSTD(1));

ALTER TABLE network_logs.nat_logs
    MODIFY COLUMN IF EXISTS session_start_time DateTime('UTC') ALIAS timestamp;
