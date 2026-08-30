# Parser

## What it extracts

From a line like:

```
firewall,info forward: in:<pppoe-P2-musa> out:vlan2436, connection-state:new,snat
proto TCP (SYN), 100.68.180.230:35420->99.124.164.160:22,
NAT (100.68.180.230:35420->103.125.177.119:35420)->99.124.164.160:22, len 60
```

it produces exactly:

```
private_ip    = 100.68.180.230      public_ip  = 103.125.177.119
private_port  = 35420               public_port = 35420
dest_ip       = 99.124.164.160      dest_port  = 22
protocol      = TCP                 subscriber_id = P2-musa
```

`timestamp` is the arrival time at the log server and `router_ip` is the
packet's source address. Neither is read from the message body — anything
inside a syslog payload is trivially spoofable.

Everything else (`firewall`, `info`, `forward`, `out:vlan2436`,
`connection-state:new`, `snat`, `SYN`, `src-mac`, `len 60`, and the whole
syslog header) is discarded and never reaches the database.

### Destination fields are not reversed

The specification calls this out and there is a dedicated regression test for
it. In `NAT (A:a->B:b)->C:c`:

- `A:a` is the private/subscriber endpoint
- `B:b` is the public/NAT endpoint
- `C:c` is the **destination**

So for the example above, `dest_ip = 99.124.164.160` and `dest_port = 22`.

## Why it is not one big regex

RouterOS emits the same event with different optional segments depending on
version, chain and rule — `src-mac` appears sometimes, `connection-state`
appears sometimes, TCP flags vary. Anchoring on the whole line is the single
most common reason home-grown MikroTik parsers break after a RouterOS upgrade.

This parser locates only the segments it needs, anywhere in the payload, and
ignores the rest. That also means the syslog header format (RFC 3164 vs
RFC 5424, with or without a hostname) does not matter.

## Supported shapes

| Shape | Example fragment |
|---|---|
| srcnat with ports | `NAT (10.0.0.1:100->1.2.3.4:200)->8.8.8.8:53` |
| srcnat without ports | `NAT (10.0.0.1->1.2.3.4)->8.8.8.8` |
| no NAT, with ports | `1.1.1.1:5->2.2.2.2:80` |
| no NAT, without ports | `1.1.1.1->2.2.2.2` |
| TCP flags | `proto TCP (SYN)`, `(FIN,ACK)`, `(PSH,ACK)`, … |
| ICMP | `proto ICMP (type 8, code 0)` — ports stored as 0 |
| Numeric protocols | `proto 47` → stored as `GRE` |
| PPPoE subscriber | `in:<pppoe-P2-musa>` → `P2-musa` |
| Tunnel subscribers | `l2tp-`, `pptp-`, `sstp-`, `ovpn-`, `pppoe-in-` prefixes |
| Bare interface | `in:ether1` → empty, or the name if configured |

Non-NAT lines are dropped by default (`parser.require_nat: true`), because a
forward log with no translation cannot answer the question this database
exists to answer. Set it to `false` to keep them; `public_ip` then equals
`private_ip`.

## Performance

`170,428 logs/sec` per core for a matching line, `3,458,083/sec` to reject a
non-matching one. Three deliberate choices get it there:

1. **A substring probe before any regex.** A line with no `NAT` in it costs one
   `str.find()`, not a backtracking match.
2. **IPv4 octet ranges validated inside the pattern.** `(?:25[0-5]|2[0-4][0-9]|
   1[0-9]{2}|[1-9]?[0-9])` runs in the regex engine's C loop. Measured 3x
   faster than matching a loose digit class and range-checking in Python.
3. **`str.find()` slicing for protocol and interface**, with a regex only as a
   fallback for uncommon spellings. Measured 2.4x faster than a third regex.

Reproduce: `python3 tests/parser/bench_parser.py`

## Adding a new log format

The parser is a pattern registry, so adding a format is two edits and no
rewrite:

1. Add the pattern to `backend/app/parser/patterns.py`.
2. Add a test case to `tests/parser/test_mikrotik.py` using a **real** captured
   line, not one you wrote from memory.
3. Wire it into `_parse()` in `backend/app/parser/mikrotik.py` if it needs new
   branching.

Capture real samples with:

```bash
sudo tcpdump -ni any -A -c 20 port 514
```

Then check what the parser currently does with one:

```bash
cd /opt/network-log-server/backend
sudo -u netlog ../venv/bin/python -c "
from app.parser import MikroTikParser
log, reason = MikroTikParser().parse(open('/tmp/sample.txt').read())
print(log or f'rejected: {reason}')"
```

## Reason codes

Rejected lines are counted by reason, so you can tell "wrong format" from
"corrupt input":

| Code | Meaning |
|---|---|
| `no_nat` | No NAT translation and `require_nat` is on |
| `no_match` | Nothing matched — unknown format, truncated, or invalid address |
| `decode` | Payload could not be decoded at all |

A malformed log never raises. The parser returns `(None, reason)` and the
receiver bumps a counter, because a crash on one bad packet would take out
ingestion for every router.
