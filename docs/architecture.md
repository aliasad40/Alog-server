# Architecture

```
   MikroTik routers
          │  syslog UDP/TCP 514
          ▼
   ┌──────────────────┐   source IP not authorised ──▶ dropped, counted
   │  Log receiver    │   unparseable ──────────────▶ dropped, counted
   │  N processes,    │
   │  SO_REUSEPORT    │
   └────────┬─────────┘
            │  msgpack rows, pipelined RPUSH
            ▼
   ┌──────────────────┐
   │  Redis queue     │  AOF everysec · noeviction · backpressure limit
   └────────┬─────────┘
            │  atomic claim into a per-worker in-flight list
            ▼
   ┌──────────────────┐
   │  Batch worker    │  retries with backoff, never drops a claim
   └────────┬─────────┘
            │  batched INSERT
            ▼
   ┌──────────────────┐        ┌──────────────┐
   │  ClickHouse      │◀───────│  Web API     │◀── nginx ◀── browser
   │  monthly parts   │        │  FastAPI     │
   └──────────────────┘        └──────┬───────┘
                                      │
                               ┌──────────────┐
                               │  SQLite      │  routers, users, settings
                               └──────────────┘
```

## Why the pieces are what they are

**Two databases, on purpose.** ClickHouse holds billions of append-only log
rows. SQLite holds a few dozen mutable rows — routers, users, settings — that
need transactions and unique constraints, which ClickHouse is bad at. Keeping
metadata out of ClickHouse is also what lets the log table be strictly
append-only, and append-only is what makes it fast.

**Redis is a durability boundary, not a cache.** If ClickHouse goes down, logs
accumulate in Redis and drain when it comes back. Redis is configured with
`appendonly yes` / `appendfsync everysec` so a power loss costs at most one
second of buffer, and `maxmemory-policy noeviction` so it never quietly throws
away queued logs to make room.

**The receiver never touches a database.** The authorised-router set is a plain
Python `set` in each receiver process, refreshed from Redis on a timer. Checking
it is a hash lookup on the hot path — no query, no lock, no round trip. This is
what makes dropping unauthorised traffic nearly free.

**Multiple receiver processes, no fan-out.** Each binds UDP/514 with
`SO_REUSEPORT` and the kernel load-balances datagrams between them. There is no
dispatcher process to become a bottleneck and no shared queue between them.

## The log-loss guarantee

This is the part that matters most, so it is worth being precise about.

A plain `LPOP` would lose data: pop 20,000 rows, fail the insert, and they are
gone. Instead the worker **claims** a batch — a Lua script atomically moves N
entries from the shared queue into a per-worker in-flight list. Entries are only
deleted once ClickHouse has acknowledged the insert.

```
LRANGE + LTRIM + RPUSH   (one atomic script)
        queue ──────────────▶ inflight:worker-1
                                    │
                          insert OK │ DEL inflight:worker-1
                          insert KO │ retry with backoff, claim retained
                          worker死  │ recovered on restart, or by the
                                    │ maintenance timer if it never returns
```

That gives **at-least-once** delivery. Duplicates are possible in exactly one
window — the insert succeeded but the ack did not — which for append-only NAT
logs is the right trade. A duplicate row is harmless. A missing row is a failed
lawful-intercept response.

What is *not* guaranteed: logs dropped by the kernel before the receiver sees
them (UDP under overload), and logs dropped deliberately when the queue exceeds
`redis.max_queue_length`. Both are counted and visible in Settings → System
status. Silent loss is the thing this design rules out; visible, counted,
back-pressured loss under genuine overload is a deliberate choice over an OOM
kill that would lose everything.

## Failure behaviour

| Failure | What happens |
|---|---|
| ClickHouse down | Worker retries with backoff up to 30s. Logs accumulate in Redis. Nothing is lost until `max_queue_length`. |
| ClickHouse slow | Queue depth grows and drains. Visible in System status. |
| Redis down | Receiver counts `dropped_redis_error` and logs an error every 10s. Ingestion resumes automatically. |
| Redis full | `noeviction` makes RPUSH fail rather than evicting. Counted as a drop, never silent. |
| Worker killed | In-flight claim is recovered on restart, or by the maintenance timer if the worker never comes back. |
| Receiver killed | systemd restarts it. Buffered records not yet pushed are lost — at most `push_interval_ms`. |
| Malformed log | Counted as `parser_errors`, discarded. Never crashes the receiver. |
| Unauthorised sender | Dropped after one hash lookup, counted as `unknown_router`. |
| Disk full | ClickHouse rejects inserts, the worker retries, the queue backs up, System status shows disk %. |
| Whole box reboots | Redis AOF replays the queue; the worker drains it. At most 1s of buffer lost. |

## Process model

| Service | Processes | Role |
|---|---|---|
| `network-log-server-receiver` | `receiver.workers` (default: cores/2) | syslog ingest |
| `network-log-server-worker` | 1 | Redis → ClickHouse |
| `network-log-server-api` | `server.workers` (default 2) | web API |
| `network-log-server-maintenance` | oneshot, daily timer | archiving, orphan recovery |

All run as the unprivileged `netlog` user. The receiver gets
`CAP_NET_BIND_SERVICE` so it can bind port 514 without being root, and the
tightest systemd sandbox of the three because it is the most network-exposed.

Running more than one worker is supported — each takes its own in-flight key —
but rarely helps. One worker doing 20,000-row batches saturates a single
ClickHouse instance long before it becomes the bottleneck.
