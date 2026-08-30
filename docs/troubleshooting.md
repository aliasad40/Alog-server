# Troubleshooting

Start here:

```bash
sudo bash /opt/network-log-server/scripts/health-check.bash
nls-admin status
```

## No logs are arriving

Work down this list in order — each step rules out one layer.

**1. Are packets reaching the box?**

```bash
sudo tcpdump -ni any port 514
```

Nothing? The problem is the router or the network, not this application. Check
`src-address` in the router's logging action and any firewall between them.

**2. Is the source address authorised?**

```bash
nls-admin list-routers
```

Then check Settings → System status. A climbing `unknown_router` counter means
packets are arriving from an address that is not in the list, or is disabled.
The address must match the router's `src-address` exactly.

**3. Is the receiver listening?**

```bash
ss -lnup 'sport = :514'
systemctl status network-log-server-receiver
```

**4. Is the parser recognising the format?**

A climbing `parser_errors` counter means logs arrive but do not match. Capture
a sample and compare against `docs/parser.md`:

```bash
sudo tcpdump -ni any -A -c 5 port 514
```

## Logs arrive but nothing is searchable

The worker is not draining Redis into ClickHouse.

```bash
systemctl status network-log-server-worker
journalctl -u network-log-server-worker -n 50
redis-cli -a "$NLS_REDIS_PASSWORD" llen nls:queue
```

A queue depth that climbs and never drains means ClickHouse is refusing
inserts. The worker logs the actual error each time it retries — read it.

```bash
systemctl status clickhouse-server
clickhouse-client --user netlog --password "$NLS_CLICKHOUSE_PASSWORD" --query "SELECT 1"
```

## Queue depth keeps growing

| Cause | Check | Fix |
|---|---|---|
| ClickHouse down | `systemctl status clickhouse-server` | Start it; the backlog drains automatically |
| ClickHouse slow | `SELECT * FROM system.merges` | Check disk I/O; consider faster storage |
| Too many parts | `parts_to_delay_insert` in the ClickHouse log | Raise `clickhouse.batch_size` — fewer, bigger inserts |
| Disk full | `df -h /var/lib/clickhouse` | Free space or reduce `retention_months` |
| Batch too small | System status → last insert ms | Raise `clickhouse.batch_size` |

The queue existing is not a problem. The queue *not draining* is.

## Kernel is dropping UDP packets

Symptom: the load generator or router sends far more than `logs_received` shows.

```bash
netstat -su | grep -iE "receive errors|buffer errors"
```

Fixes, in order of effect:

1. Raise the socket buffer. Check the actual value the kernel granted:
   ```bash
   journalctl -u network-log-server-receiver | grep rcvbuf
   ```
   If it is lower than `receiver.so_rcvbuf`, `net.core.rmem_max` is capping it:
   ```bash
   sysctl -w net.core.rmem_max=33554432   # install.bash sets this persistently
   ```
2. Add receiver processes — raise `receiver.workers` in the config and restart.
3. Move to TCP transport on the routers if the volume genuinely exceeds what
   UDP can deliver on your hardware.

## Cannot sign in

**"Too many failed attempts."** Login throttling, five failures per
username+IP. Wait five minutes, or clear it:

```bash
redis-cli -a "$NLS_REDIS_PASSWORD" --scan --pattern 'nls:login_fail:*' | \
  xargs -r redis-cli -a "$NLS_REDIS_PASSWORD" del
```

**Lost the admin password.** Reset it:

```bash
sudo nls-admin create-admin admin --force
```

**Session drops immediately.** Usually a changed `NLS_SECRET_KEY`, which
invalidates every existing session. That is expected after restoring an old
backup over a newer config.

## Services will not start

```bash
journalctl -u network-log-server-api -n 50 --no-pager
```

**`NLS_SECRET_KEY must be at least 32 characters`** — `/etc/network-log-server/.env`
is missing or unreadable by the `netlog` user:

```bash
ls -l /etc/network-log-server/.env     # want: -rw-r----- root netlog
```

**`Permission denied`** on data or logs:

```bash
sudo chown -R netlog:netlog /var/lib/network-log-server /var/log/network-log-server
```

**Port 514 already in use** — usually rsyslog:

```bash
sudo ss -lnup 'sport = :514'
sudo systemctl disable --now rsyslog     # if you do not need it
```

## Searches are slow or time out

The search API caps execution at `clickhouse.max_execution_time` (30s).

- **Always give a narrow time range.** It is the single biggest factor.
- **Filter on `public_ip` where you can** — that is the leading sorting key.
- **Avoid partial subscriber match** on wide ranges; it cannot use the bloom
  filter.
- **Use "Count all" sparingly.** Counting every match is the expensive half of
  a search, which is why it is a separate button.

## Disk filling up

```bash
nls-admin status
```

Options, cheapest first:

1. Reduce `retention_months` in Settings → Retention. Takes effect on the next
   TTL merge.
2. Reduce `hot_days` so partitions recompress at ZSTD(9) sooner.
3. Enable `retention.drop_after_archive` — but restore an archive first to
   prove they work.
4. Reduce what the routers log. See "Volume planning" in `docs/mikrotik.md`.

## Getting help

Include this when reporting a problem:

```bash
nls-admin status
systemctl status network-log-server-{receiver,worker,api} --no-pager
journalctl -u network-log-server-receiver -n 100 --no-pager
sudo tcpdump -ni any -A -c 3 port 514        # redact addresses as needed
```
