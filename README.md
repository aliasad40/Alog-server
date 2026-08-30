# Network Log Server

High-performance, open-source NAT/firewall log platform for MikroTik routers.
Built for ISPs that have to answer one question quickly and correctly:

> At 18:33 on 30 August, which subscriber was behind 103.125.177.119:35420?

It receives syslog from authorised routers, parses MikroTik CGNAT logs into a
normalised schema, buffers through Redis so nothing is lost when the database
is busy, and stores the result in ClickHouse with aggressive per-column
compression. A deliberately small web interface does two things: search logs and
manage routers.

```
Clone → Install → Sign in → Add routers → Collect logs.
```

## Quick install

Ubuntu 24.04 LTS:

```bash
git clone <repository-url>
cd network-log-server
sudo bash install.bash
```

Then open `http://<server-ip>/` and sign in with the credentials the installer
prints.

One command installs and configures ClickHouse, Redis, nginx, the Python
services, systemd units, the database schema, an admin account and a firewall
policy — then health-checks all of it.

## What it does

- **Receives** syslog over UDP and TCP on port 514
- **Authorises** by source address — unknown senders are dropped after one hash
  lookup, before the parser ever runs
- **Parses** MikroTik firewall/NAT logs into 11 normalised columns and discards
  everything else
- **Buffers** through Redis so ClickHouse can be down, slow or restarting
  without losing logs
- **Stores** in ClickHouse, partitioned monthly, compressed per column
- **Searches** on any field with a time range, from a two-tab web interface
- **Archives** closed months automatically, recompresses old partitions in place
- **Reports** ingest rate, queue depth, parse success, drops and storage

## Measured performance

On a **single vCPU** Xeon @ 2.80 GHz — deliberately small hardware:

| | Result |
|---|---|
| Parser, one core | **170,428 logs/sec** |
| Non-matching line rejection | **3,458,083 lines/sec** |
| End-to-end ingest, one receiver process | **40,000 logs/sec at 100% delivery, 100% parse** |

The receiver scales across cores with `SO_REUSEPORT`, so 8 dedicated cores
should carry well past 100,000 logs/sec. Measure it on your own hardware with
`scripts/loadtest.py` before promising it to anyone. Full methodology and the
overload failure mode: [`docs/performance.md`](docs/performance.md).

## Architecture

```
MikroTik routers ──syslog──▶ Receiver ──▶ Redis queue ──▶ Worker ──▶ ClickHouse
                             (N procs,     (durable       (batched     (monthly
                              authorise,    buffer)        inserts)     partitions,
                              parse)                                    compressed)
                                                                            │
                                       Browser ──▶ nginx ──▶ Web API ───────┘
```

Two databases on purpose: ClickHouse for billions of append-only log rows,
SQLite for the few dozen mutable rows (routers, users, settings) that need
transactions and unique constraints. See
[`docs/architecture.md`](docs/architecture.md).

### The log-loss guarantee

The worker *claims* a batch rather than popping it — a Lua script atomically
moves entries into a per-worker in-flight list, and they are only deleted once
ClickHouse acknowledges the insert. A killed worker's claim is recovered on
restart, or by the maintenance timer if it never comes back.

That gives at-least-once delivery. A duplicate row is harmless; a missing one
is a failed lawful-intercept response.

## Database design

```sql
ENGINE = MergeTree
PARTITION BY toYYYYMM(timestamp)
ORDER BY (public_ip, public_port, timestamp)
TTL timestamp + INTERVAL 30 DAY RECOMPRESS CODEC(ZSTD(9)),
    timestamp + INTERVAL 12 MONTH DELETE
```

- `public_ip` leads the sorting key because the reverse CGNAT lookup is the
  query that matters, and a NAT pool of a few /24s compresses to nearly nothing
- `session_start_time` is an `ALIAS` of `timestamp` — searchable, zero bytes
- `DoubleDelta` on timestamps, `LowCardinality` on subscriber and protocol,
  `UInt16` ports, `IPv4` addresses
- Bloom filter skip indexes for subscriber, private IP and destination IP

Details and reasoning: [`docs/database.md`](docs/database.md).

## Documentation

| Document | Covers |
|---|---|
| [installation.md](docs/installation.md) | Requirements, install, HTTPS, upgrade, uninstall |
| [mikrotik.md](docs/mikrotik.md) | RouterOS configuration, volume planning |
| [architecture.md](docs/architecture.md) | Components, log-loss guarantee, failure behaviour |
| [parser.md](docs/parser.md) | Supported formats, adding new ones |
| [database.md](docs/database.md) | Schema, retention, archiving, useful queries |
| [performance.md](docs/performance.md) | Benchmarks, load testing, tuning |
| [api.md](docs/api.md) | REST reference |
| [troubleshooting.md](docs/troubleshooting.md) | Symptom-first diagnosis |

## Configuration

Two files. Tunables in `/etc/network-log-server/log-server.yaml`, secrets in
`/etc/network-log-server/.env` (0640, root:netlog, generated at install).
Everything is validated at startup — a service with a bad config exits rather
than starting up half-configured.

The knobs you are most likely to touch:

```yaml
receiver:
  workers: 4              # SO_REUSEPORT processes; raise for more throughput
  so_rcvbuf: 16777216     # raise net.core.rmem_max to match
parser:
  require_nat: true       # drop forward logs with no NAT translation
clickhouse:
  batch_size: 20000       # bigger batches, fewer parts
retention:
  hot_days: 30            # recompress after this
  retention_months: 12    # delete after this
```

## Administration

```bash
nls-admin status              # rows, disk, compression, partitions
nls-admin list-routers
nls-admin add-router LHE-BRAS-01 10.10.10.1
nls-admin create-admin admin --force    # reset a lost password
nls-admin archive             # run the monthly archive now
nls-admin apply-retention --hot-days 14 --retention-months 6

scripts/health-check.bash     # all components, exit 0 when healthy
scripts/backup.bash           # config, users, routers, branding
scripts/loadtest.py --host <ip> --rate 50000 --duration 60
```

## Security

- Argon2id password hashing (64 MiB, 3 iterations) — never plaintext
- Sessions in `HttpOnly`, `SameSite=Strict` cookies, plus an `Origin` check on
  mutations
- Login throttling per username+IP, plus rate limiting at nginx
- Every search value bound as a ClickHouse parameter; `ORDER BY` restricted to
  an allow-list
- Uploaded logos decoded and re-encoded to PNG — what is served back is
  something Pillow generated, not something a user supplied. SVG is refused
- Services run as unprivileged `netlog` under a strict systemd sandbox; the
  receiver binds port 514 via `CAP_NET_BIND_SERVICE`, not root
- Secrets generated at install, never committed, never defaulted
- ClickHouse and Redis bind loopback only

## Development

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r backend/requirements-dev.txt

redis-server --port 6399 --save '' --appendonly no --daemonize yes
pytest -q                                  # 73 tests
python3 tests/parser/bench_parser.py       # parser throughput
```

Tests are grouped by what they protect:

- `tests/parser/` — every supported log shape, plus garbage that must not crash
  the receiver
- `tests/integration/` — real Redis: authorisation, the claim/recover cycle,
  backpressure, real UDP datagrams
- `tests/api/` — auth, lockout, CSRF, router CRUD, and query-injection attempts
  asserted against the exact generated SQL

## Project layout

```
install.bash  update.bash  uninstall.bash
backend/app/
  parser/      MikroTik log parsing (patterns are a registry, not a monolith)
  queue/       Redis buffer with atomic claim/ack
  database/    ClickHouse access + SQLite metadata
  services/    receiver, worker, maintenance
  api/         FastAPI endpoints
  auth/        Argon2id, JWT, login throttling
frontend/      three static files, no build step, no CDN
database/schema/clickhouse.sql
systemd/  nginx/  scripts/  tests/  docs/
```

The frontend is intentionally vanilla HTML/CSS/JS. There is no bundler and no
`npm install` in the install path, which keeps Node out of the deployment
entirely and means this installs on air-gapped ISP networks.

## License

MIT. See [LICENSE](LICENSE).

---

Created by Ali Asad
