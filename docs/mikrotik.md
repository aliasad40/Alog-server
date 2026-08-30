# Configuring MikroTik routers

Verify these commands against your RouterOS version before rolling them out
fleet-wide. They are written for RouterOS 6.4x and 7.x, which differ in a few
places noted below.

## 1. Point the router at the log server

```
/system logging action
add name=logserver target=remote remote=192.0.2.10 remote-port=514 \
    src-address=10.10.10.1 bsd-syslog=yes syslog-facility=local0
```

`src-address` is the important one. The log server authorises by source
address, so this must match exactly what you enter in the Routers tab. If the
router has several interfaces, leaving it unset means the address changes with
routing and your logs start being discarded.

To use TCP instead of UDP (RouterOS 7 only):

```
/system logging action set logserver remote-protocol=tcp
```

UDP is the default and is what most operators use. TCP costs the router more
state but does not lose messages to a full socket buffer.

## 2. Send firewall logs

```
/system logging
add topics=firewall action=logserver
```

To reduce volume, send only what you need:

```
/system logging add topics=firewall,info action=logserver
```

## 3. Log the NAT rule

Logging is enabled per firewall rule, not globally. On the srcnat rule that
does your CGNAT translation:

```
/ip firewall nat
set [find action=src-nat] log=yes log-prefix=""
```

Leave `log-prefix` empty. The parser does not need it, and a prefix on every
line is bandwidth and disk you pay for on every log forever.

For connection-tracking-based logging on the forward chain:

```
/ip firewall filter
add chain=forward connection-state=new action=accept log=yes \
    comment="log new sessions for CGNAT records"
```

Put this rule where it will actually be hit — before any rule that already
accepts the traffic.

## 4. Confirm the format

```
/log print where topics~"firewall"
```

You should see lines like:

```
firewall,info forward: in:<pppoe-P2-musa> out:vlan2436, connection-state:new,snat
proto TCP (SYN), 100.68.180.230:35420->99.124.164.160:22,
NAT (100.68.180.230:35420->103.125.177.119:35420)->99.124.164.160:22, len 60
```

The parser needs three things from this line:

| Needed | Looks like | Used for |
|---|---|---|
| The NAT tuple | `NAT (private:port->public:port)->dest:port` | the five address/port columns |
| The inbound interface | `in:<pppoe-USERNAME>` | `subscriber_id` |
| The protocol | `proto TCP` | `protocol` |

Everything else on the line is discarded and never reaches the database.

If your interface is not PPPoE, `subscriber_id` will be empty. Set
`parser.subscriber_from_interface: true` in `/etc/network-log-server/log-server.yaml`
to store the raw interface name instead.

## 5. Authorise the router in the web interface

Routers tab → **Add router**:

- **Name** — anything you will recognise, e.g. `LHE-BRAS-01`
- **IP address** — the `src-address` from step 1
- **Description** — optional

Until you do this, every log from that router is discarded and counted as
`unknown_router`. That is deliberate.

## 6. Open the firewall to that router only

On the log server:

```bash
sudo ufw allow from 10.10.10.1 to any port 514 proto udp
sudo ufw allow from 10.10.10.1 to any port 514 proto tcp
```

`install.bash` does not open 514 to the world for you. Application-level
authorisation still applies either way, but there is no reason to let the
internet reach the syslog socket at all.

## Volume planning

A busy BRAS logging every new connection produces a lot of syslog. Before
enabling it on a full subscriber base, enable it on one and measure:

```bash
# on the log server
nls-admin status          # rows and bytes on disk
```

If the volume is more than you want, the usual reductions in order of
effectiveness:

1. Log only the srcnat rule, not the whole forward chain.
2. Log only `connection-state=new` — you need one record per session, not per
   packet.
3. Exclude traffic you will never be asked about (internal ranges, your own
   DNS resolvers) with an earlier non-logging accept rule.

## Troubleshooting

**Nothing arrives.** Confirm the router is sending and the packets reach the box:

```bash
sudo tcpdump -ni any port 514 and host 10.10.10.1
```

If tcpdump sees nothing, the problem is on the router or in the network. If it
sees traffic but the counters do not move, the source address is not authorised —
check `unknown_router` in Settings → System status.

**Logs arrive but `parser_errors` climbs.** The format is not one the parser
recognises. Capture a sample and compare it against `docs/parser.md`:

```bash
sudo tcpdump -ni any -A -c 5 port 514
```

**Counters move but nothing is searchable.** The worker is not draining Redis:

```bash
systemctl status network-log-server-worker
journalctl -u network-log-server-worker -n 50
```
