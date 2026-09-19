# Changelog

All notable changes to this project are documented here.
This project follows [Semantic Versioning](https://semver.org/).

## [1.0.1] — 2026-08-31

### Fixed
- **Installer failed at step 8/15 with `systemctl restart redis-server`.**
  `write_configuration` set `umask 077` without scoping it. A bare `umask` is
  process-global, so it leaked into `configure_redis` and created
  `/etc/redis/redis.conf.d-nls.conf` as `0600 root:root`. The `redis` user
  could not read the file its own config `include`d, so Redis refused to start
  and the installation aborted.

  Three changes:
  - `umask 077` in `write_configuration` and `create_admin_account` now runs
    inside a subshell, so it cannot affect files created later.
  - `configure_redis` sets `chown root:redis` and `chmod 0640` on the drop-in
    explicitly, rather than relying on whatever umask is in effect. The file
    holds the Redis password, so it must be unreadable to others but readable
    by Redis.
  - Same reasoning applied to the generated credentials file.

  Existing installations that hit this can recover without reinstalling:

      sudo chown root:redis /etc/redis/redis.conf.d-nls.conf
      sudo chmod 0640 /etc/redis/redis.conf.d-nls.conf
      sudo systemctl restart redis-server
      sudo bash install.bash        # choose 2) Repair

## [1.0.0] — 2026-08-31

First production release.

### Ingestion
- Async syslog receiver, UDP and TCP on port 514
- Multi-process scaling via `SO_REUSEPORT` — the kernel load-balances
  datagrams, no user-space fan-out
- Source-address authorisation checked against an in-memory set; unauthorised
  senders are dropped after one hash lookup, before the parser runs
- Modular MikroTik parser: srcnat with and without ports, TCP flag variants,
  ICMP, numeric protocols, PPPoE and tunnel subscriber extraction
- Measured 170,428 logs/sec/core parsing; 40,000 logs/sec end-to-end at 100%
  delivery on a single vCPU

### Buffering
- Redis queue with atomic claim/ack — batches are only deleted after ClickHouse
  acknowledges the insert, giving at-least-once delivery
- Crash recovery for in-flight claims, both on worker restart and via the
  maintenance timer for workers that never return
- Backpressure at a configurable queue depth: load is shed and counted rather
  than pushing Redis into OOM
- msgpack wire format, roughly half the size of JSON

### Storage
- ClickHouse `MergeTree`, monthly partitions, sorting key
  `(public_ip, public_port, timestamp)` for the CGNAT reverse lookup
- Per-column codecs: `DoubleDelta` on timestamps, `ZSTD` elsewhere,
  `LowCardinality` for subscriber and protocol
- `session_start_time` as a zero-byte `ALIAS` of `timestamp`
- Bloom filter skip indexes on subscriber, private IP and destination IP
- TTL recompression to `ZSTD(9)` after `hot_days`; deletion after
  `retention_months`

### Archiving
- Daily maintenance timer exports closed months to gzip-compressed ClickHouse
  Native files
- Archive status, row counts and failures recorded and exposed via the API
- Ingestion is never paused during an archive

### Interface
- Two-tab web interface: Log Search and Routers
- Lookup presets that keep each search on an index
- Search on any field, with pagination, sorting, CSV export and an optional
  exact count
- Router management with immediate effect on the receiver allow-list
- Configurable company name and logo
- System status: ingest rates, queue depth, parse success, drops, storage,
  compression ratio, host resources
- "Created by Ali Asad" in the footer of every page

### Security
- Argon2id password hashing (64 MiB, 3 iterations, 4 lanes)
- JWT sessions in `HttpOnly`, `SameSite=Strict` cookies with an `Origin` check
  on mutations
- Login throttling per username+IP, plus nginx rate limiting
- All search values bound as ClickHouse parameters; `ORDER BY` allow-listed
- Uploaded logos decoded and re-encoded to PNG; SVG refused
- Services run unprivileged under a strict systemd sandbox;
  `CAP_NET_BIND_SERVICE` instead of root for port 514
- Secrets generated at install time, never defaulted or committed

### Operations
- `install.bash` — one-command install on Ubuntu 24.04 with 15 verified steps,
  idempotent, offering upgrade/repair on re-run
- `update.bash` — backed-up, migrated, health-checked, with rollback files kept
  on failure
- `uninstall.bash` — preserves data by default; deletion requires typing DELETE
- `nls-admin` CLI for database, routers, admin accounts, retention and archives
- `scripts/health-check.bash`, `backup.bash`, `restore.bash`, `loadtest.py`
- Docker Compose for evaluation

### Tests
- 73 tests: parser shapes and garbage handling, real-Redis integration
  (authorisation, claim/recover, backpressure, live UDP), API auth, CSRF,
  router CRUD and query-injection attempts asserted against generated SQL
