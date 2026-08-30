-- Network Log Server -- ClickHouse schema
--
-- Placeholders {{DATABASE}}, {{HOT_DAYS}} and {{RETENTION_MONTHS}} are
-- substituted by `nls-admin init-db` (which install.bash calls).
--
-- ---------------------------------------------------------------------------
-- Why the table looks like this
-- ---------------------------------------------------------------------------
--
-- ORDER BY (public_ip, public_port, timestamp)
--   The query this database exists to answer is the CGNAT reverse lookup:
--   "at 18:33 on 30 August, which subscriber was behind 103.125.177.119:35420?"
--   Putting the public endpoint first makes that an index seek instead of a
--   scan, and because an ISP NAT pool is a handful of /24s, public_ip arrives
--   in long identical runs -- it compresses to almost nothing and it prunes
--   hard. Other lookups (by subscriber, private IP, destination) are served
--   by the skip indexes below.
--
-- PARTITION BY toYYYYMM(timestamp)
--   One partition per month, as specified. Monthly archive, retention and
--   deletion all become metadata operations on whole partitions rather than
--   row-level work.
--
-- session_start_time is an ALIAS, not a stored column
--   The specification defines it as identical to the receive timestamp.
--   Storing the same value twice would cost real disk on billions of rows,
--   so it is computed at query time: fully searchable, zero bytes written.
--
-- Codecs are chosen per column, not blanket-applied
--   DoubleDelta on timestamps (near-monotonic arrival -> a few bits per row),
--   ZSTD on everything else, LowCardinality dictionaries for the two columns
--   with repeated string values.
--
-- TTL ... RECOMPRESS is how "archiving" works in place
--   After hot_days the partition is rewritten at ZSTD(9): materially smaller,
--   still fully searchable, no application involvement. Rows are dropped after
--   the retention window. Export-to-file archiving is separate (scripts/archive.bash).

CREATE DATABASE IF NOT EXISTS {{DATABASE}};

CREATE TABLE IF NOT EXISTS {{DATABASE}}.nat_logs
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

    -- Secondary lookup paths. Bloom filters let ClickHouse skip whole
    -- granules for equality searches on columns that are not in the sorting
    -- key, which is what every non-CGNAT search in the UI does.
    INDEX idx_subscriber subscriber_id TYPE bloom_filter(0.01) GRANULARITY 4,
    INDEX idx_private    private_ip    TYPE bloom_filter(0.01) GRANULARITY 4,
    INDEX idx_dest       dest_ip       TYPE bloom_filter(0.01) GRANULARITY 4,
    INDEX idx_router     router_ip     TYPE set(64)            GRANULARITY 4,
    -- Parts are written in arrival order, so a minmax on the timestamp prunes
    -- most parts for a narrow time window even though time is not the leading
    -- sorting key.
    INDEX idx_ts         timestamp     TYPE minmax             GRANULARITY 1
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(timestamp)
ORDER BY (public_ip, public_port, timestamp)
TTL timestamp + INTERVAL {{HOT_DAYS}} DAY RECOMPRESS CODEC(ZSTD(9)),
    timestamp + INTERVAL {{RETENTION_MONTHS}} MONTH DELETE
SETTINGS index_granularity = 8192,
         ttl_only_drop_parts = 1,
         merge_with_ttl_timeout = 3600;
