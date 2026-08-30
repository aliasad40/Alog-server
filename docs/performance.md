# Performance

Every number here was measured, not estimated. The methodology is included so
you can reproduce it on your own hardware, which is the only measurement that
matters for capacity planning.

## Reference measurements

Measured on a **1 vCPU** Intel Xeon @ 2.80 GHz container, Python 3.12,
Ubuntu 24.04. This is deliberately small hardware — treat these as a floor,
not a ceiling.

### Parser, single core

```
  170,428 logs/sec      full parse of a matching CGNAT line (5.87 us each)
3,458,083 lines/sec     rejection of non-matching lines (fast substring probe)
```

Reproduce with:

```bash
python3 tests/parser/bench_parser.py
```

The rejection number matters more than it looks. It is what makes the
source-IP allow-list cheap: traffic from an unauthorised host is dropped after
one hash lookup, and traffic that is not a NAT log is dropped after one
`str.find()`. Neither costs a regex.

### End-to-end ingestion: UDP socket → parse → Redis

One receiver process, with the load generator competing for the *same single
core*:

| Offered rate | Sent      | Received  | Delivered | Parsed |
|--------------|-----------|-----------|-----------|--------|
| 5,000/s      | 39,450    | 39,450    | 100.0%    | 100.0% |
| 10,000/s     | 78,700    | 78,700    | 100.0%    | 100.0% |
| 20,000/s     | 157,000   | 157,000   | 100.0%    | 100.0% |
| 40,000/s     | 315,600   | 315,600   | 100.0%    | 100.0% |
| unlimited    | 1,236,000 | 282,405   | 22.8%     | 100.0% |

**40,000 logs/sec sustained with zero loss on one core, sharing that core with
the traffic generator.** Note that "parsed" stays at 100% even in the overload
row: nothing is silently mangled. What is lost at overload is lost in the
kernel's UDP receive queue before the application ever sees it, and it shows up
in `netstat -su`.

The `unlimited` row is the useful failure case. It is what an under-provisioned
box looks like, and both causes are fixable:

1. `net.core.rmem_max` was 4 MiB on the test box, so the requested 32 MiB
   `SO_RCVBUF` was silently clamped. `install.bash` raises this to 32 MiB.
2. One core cannot both generate 154,000 packets/sec and receive them.

### Scaling

The receiver runs `receiver.workers` processes that each bind UDP/514 with
`SO_REUSEPORT`. The kernel hashes each datagram to one socket, so the processes
never contend and there is no user-space fan-out. Scaling is close to linear in
cores until you reach the NIC or Redis.

Extrapolating the single-core figure, **8 dedicated receiver cores should carry
well past 100,000 logs/sec**. Measure it on your hardware before promising it to
anyone — that is what `scripts/loadtest.py` is for.

## Running your own load test

Run it from a **different machine** than the log server, so the generator does
not compete for CPU with the thing it is measuring:

```bash
python3 scripts/loadtest.py --host <server-ip> --rate 50000 --duration 60
```

The source address must be authorised in the Routers tab, or every packet is
counted as `unknown_router` and discarded. (Verifying that it *is* discarded is
itself a worthwhile test.)

Then compare the generator's output against the server's own counters:

```bash
nls-admin status
# or Settings → System status in the web interface
```

Watch these while it runs:

```bash
watch -n1 'redis-cli -a "$NLS_REDIS_PASSWORD" llen nls:queue'   # queue depth
netstat -su | grep -iE "receive errors|buffer errors"           # kernel drops
clickhouse-client --query "SELECT count() FROM network_logs.nat_logs"
```

### Reading the result

| Symptom | Meaning | Fix |
|---|---|---|
| Generator sent ≫ server received | Kernel dropped UDP | Raise `net.core.rmem_max` and `receiver.so_rcvbuf`; add `receiver.workers` |
| Queue depth climbs and stays up | ClickHouse cannot keep up | Raise `clickhouse.batch_size`; check disk write throughput |
| Queue depth spikes then drains | Normal | Nothing — this is the buffer doing its job |
| `parser_errors` climbing | Log format not recognised | Capture a sample and add a pattern; see `docs/parser.md` |
| `unknown_router` climbing | Sender is not authorised | Add it in the Routers tab |

## Storage

Storage decides whether this is affordable, and it depends on your traffic mix.
Measure it on your own data after a day of ingestion:

```bash
nls-admin status
```

which reports rows, bytes on disk, the compression ratio and bytes per row.

What drives the ratio, in order of impact:

- **`public_ip` is the leading sorting key.** An ISP NAT pool is a handful of
  /24s, so this column arrives in long identical runs and compresses to almost
  nothing. An unusually large pool means a lower ratio.
- **`timestamp` uses `DoubleDelta`.** Near-monotonic arrival compresses to a
  few bits per row.
- **`subscriber_id` and `protocol` are `LowCardinality`.** Stored once in a
  per-part dictionary rather than once per row.
- **`session_start_time` costs zero bytes.** It is an `ALIAS` of `timestamp`,
  computed at query time.
- **Nothing is stored raw.** No `src-mac`, no interface names, no packet
  lengths, no original syslog text.

After `retention.hot_days` the TTL rewrites older partitions at `ZSTD(9)`,
shrinking them further. That data stays fully searchable.

## Query performance

The sorting key `(public_ip, public_port, timestamp)` makes the CGNAT reverse
lookup — the query this database exists for — an index seek:

```sql
SELECT subscriber_id, private_ip, timestamp
FROM network_logs.nat_logs
WHERE public_ip = '103.125.177.119'
  AND public_port = 35420
  AND timestamp BETWEEN '2026-08-30 18:00:00' AND '2026-08-30 19:00:00';
```

Searches on `subscriber_id`, `private_ip` and `dest_ip` are served by bloom
filter skip indexes, which let ClickHouse skip whole granules instead of
scanning them.

The search UI always sends a time range and the API enforces one. That is not a
cosmetic limit: without it, one careless search scans the entire retention
period and evicts the page cache for everyone else on the box.

Use **Count all** sparingly. Counting every match over a wide range is the
expensive half of a search, which is why it is a separate button and a separate
endpoint rather than something every query pays for.
