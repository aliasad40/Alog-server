# Database design

## Schema

```sql
CREATE TABLE network_logs.nat_logs
(
    timestamp          DateTime               CODEC(DoubleDelta, ZSTD(1)),
    router_ip          IPv4                   CODEC(ZSTD(1)),
    subscriber_id      LowCardinality(String) CODEC(ZSTD(1)),
    private_ip         IPv4                   CODEC(ZSTD(1)),
    private_port       UInt16                 CODEC(ZSTD(1)),
    public_ip          IPv4                   CODEC(ZSTD(1)),
    public_port        UInt16                 CODEC(ZSTD(1)),
    dest_ip            IPv4                   CODEC(ZSTD(1)),
    dest_port          UInt16                 CODEC(ZSTD(1)),
    protocol           LowCardinality(String) CODEC(ZSTD(1)),
    session_end_time   Nullable(DateTime)     CODEC(DoubleDelta, ZSTD(1)),
    session_start_time DateTime ALIAS timestamp,
    ...
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(timestamp)
ORDER BY (public_ip, public_port, timestamp)
TTL timestamp + INTERVAL 30 DAY RECOMPRESS CODEC(ZSTD(9)),
    timestamp + INTERVAL 12 MONTH DELETE
```

Full file with commentary: `database/schema/clickhouse.sql`.

## The decisions that matter

### `ORDER BY (public_ip, public_port, timestamp)`

The query this database exists to answer is the CGNAT reverse lookup: *at
18:33 on 30 August, which subscriber was behind 103.125.177.119:35420?* Putting
the public endpoint first makes that an index seek rather than a scan.

It pays twice. An ISP NAT pool is a handful of /24s, so `public_ip` arrives in
long identical runs — it compresses to almost nothing *and* it prunes hard.

Other lookups (by subscriber, private IP, destination) are served by bloom
filter skip indexes, which let ClickHouse skip whole granules instead of
reading them.

### `session_start_time` is an ALIAS, not a column

The specification defines it as identical to the receive timestamp. Storing the
same value twice would cost real disk across billions of rows, so it is
computed at query time: fully searchable, zero bytes written.

### Smallest types that fit

`UInt16` for ports (not `UInt32`), `IPv4` for addresses (4 bytes, not a 15-byte
string), `LowCardinality(String)` for the two columns with repeated values.
This is section 15's "smallest appropriate data types" taken literally.

### Codecs chosen per column

`DoubleDelta` on timestamps, because near-monotonic arrival compresses to a few
bits per row. `ZSTD(1)` elsewhere. Applying one codec blindly to every column
would leave most of the compression on the table.

### `PARTITION BY toYYYYMM(timestamp)`

One partition per month, as specified. Monthly archive, retention and deletion
all become metadata operations on whole partitions rather than row-level work.
`ttl_only_drop_parts = 1` means expiry drops whole parts instead of rewriting
them, which is dramatically cheaper.

### IPv4 only

MikroTik CGNAT logging is IPv4 by nature — there is no NAT to record for IPv6.
An IPv6 line will not match the parser and is counted as `no_match`. Supporting
it would mean widening four columns to 16 bytes each to store addresses that,
by definition, carry no translation.

## Retention and archiving

Three separate mechanisms, often confused:

**1. Recompression (`hot_days`, default 30).** After this many days a partition
is rewritten at `ZSTD(9)`. It gets materially smaller and stays fully
searchable. ClickHouse does this in the background; ingestion is unaffected.

**2. Deletion (`retention_months`, default 12).** Rows older than this are
dropped. Configurable from Settings → Retention, which issues an
`ALTER TABLE ... MODIFY TTL`.

**3. Export archiving (the maintenance timer, daily at 03:17).** Closed months
are exported to `/var/backups/network-log-server/archive/` as gzip-compressed
ClickHouse Native files. The current month is never archived. Ingestion is never
paused — reading a partition takes no locks that block inserts.

Set `retention.drop_after_archive: true` only once you actually trust your
archives and have restored one.

Archive status is visible in `nls-admin status` and via `/api/system/archives`.
Failures are recorded with the error and retried on the next run.

### Restoring an archive

```bash
zcat /var/backups/network-log-server/archive/nat_logs-202608.native.gz | \
  clickhouse-client --user netlog --password "$NLS_CLICKHOUSE_PASSWORD" \
  --query "INSERT INTO network_logs.nat_logs FORMAT Native"
```

Native format round-trips types exactly, which CSV does not.

## Backing up the log database

`scripts/backup.bash` deliberately does **not** back up ClickHouse. At ISP
volume it is too large for a tar file, and copying its data directory while the
server is running produces a corrupt copy.

Use ClickHouse's own mechanism:

```sql
-- consistent snapshot, hardlinked, cheap
BACKUP TABLE network_logs.nat_logs TO Disk('backups', 'nat_logs_2026_08');
```

Or export month by month, which is what the maintenance timer already does.

## Useful queries

```sql
-- Reverse CGNAT lookup: who was behind this address and port?
SELECT timestamp, subscriber_id, private_ip, private_port, dest_ip, dest_port
FROM network_logs.nat_logs
WHERE public_ip = '103.125.177.119' AND public_port = 35420
  AND timestamp BETWEEN '2026-08-30 18:00:00' AND '2026-08-30 19:00:00'
ORDER BY timestamp;

-- Everything one subscriber did in a window
SELECT timestamp, public_ip, public_port, dest_ip, dest_port, protocol
FROM network_logs.nat_logs
WHERE subscriber_id = 'P2-musa'
  AND timestamp >= now() - INTERVAL 1 DAY
ORDER BY timestamp DESC LIMIT 500;

-- Storage and compression, per month
SELECT partition,
       formatReadableQuantity(sum(rows))                  AS rows,
       formatReadableSize(sum(data_compressed_bytes))     AS on_disk,
       round(sum(data_uncompressed_bytes) /
             sum(data_compressed_bytes), 2)               AS ratio,
       round(sum(data_compressed_bytes) / sum(rows), 2)   AS bytes_per_row
FROM system.parts
WHERE database = 'network_logs' AND table = 'nat_logs' AND active
GROUP BY partition ORDER BY partition DESC;

-- Per-column compression: where is the disk actually going?
SELECT name,
       formatReadableSize(sum(data_compressed_bytes))   AS compressed,
       round(sum(data_uncompressed_bytes) /
             sum(data_compressed_bytes), 1)             AS ratio
FROM system.parts_columns
WHERE database = 'network_logs' AND table = 'nat_logs' AND active
GROUP BY name ORDER BY sum(data_compressed_bytes) DESC;
```

## Changing the schema

Add a numbered file to `database/migrations/` using `ALTER TABLE ... IF NOT
EXISTS`. `update.bash` applies them in order after re-running `init-db`, which
is itself idempotent (`CREATE ... IF NOT EXISTS` throughout).

Never write a migration that drops or rewrites a column on a populated table
without measuring it first. On billions of rows a `MODIFY COLUMN` is a full
rewrite of that column across every partition.
