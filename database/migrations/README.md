# Migrations

`update.bash` applies every `*.sql` file in this directory in filename order,
after re-running `init-db` (which is itself idempotent — `CREATE ... IF NOT
EXISTS` throughout).

Name them so they sort correctly:

```
001-add-something.sql
002-widen-something-else.sql
```

## Rules

**Always use `IF NOT EXISTS` / `IF EXISTS`.** Migrations re-run on repair and on
every upgrade, so they must be safe to apply twice.

```sql
ALTER TABLE network_logs.nat_logs
    ADD COLUMN IF NOT EXISTS bytes_out UInt32 CODEC(ZSTD(1));

ALTER TABLE network_logs.nat_logs
    ADD INDEX IF NOT EXISTS idx_dest_port dest_port TYPE set(256) GRANULARITY 4;
```

**Never drop or rewrite a column on a populated table without measuring it
first.** On billions of rows a `MODIFY COLUMN` is a full rewrite of that column
across every partition — hours of I/O that competes with live ingestion.

**Adding a column is cheap; changing one is not.** ClickHouse fills a new
column with its default lazily, so `ADD COLUMN` is close to a metadata
operation.

**Test against a copy with real volume**, not an empty table. The behaviour
that bites is always about part count and merge pressure, and neither exists on
an empty table.

There are no migrations yet — v1.0.0 is the initial schema.
