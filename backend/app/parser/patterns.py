"""Regex pattern registry for MikroTik firewall/NAT log lines.

Adding support for a new log variation means adding one entry here and one
test case in tests/parser/. Nothing else in the application changes.

Design notes
------------
* We never try to match a whole log line. RouterOS emits the same event with
  different optional segments (src-mac, connection-state, TCP flags, hardware
  offload markers...) depending on version, chain and rule. Anchoring on the
  full line is the single most common reason home-grown MikroTik parsers
  break after a RouterOS upgrade. Instead we locate the two or three segments
  we actually need, anywhere in the payload.

* The octet ranges are enforced *inside* the pattern rather than by a
  follow-up check in Python. Measured on the reference box this is ~3x faster
  than matching a loose digit class and validating afterwards, because the range check
  runs in the regex engine's C loop and a malformed address is rejected
  without ever allocating a Python string. See tests/parser/bench_parser.py.

* The syslog header (<PRI>, BSD timestamp, hostname, tag) is ignored
  completely. The event time we store is the packet arrival time on the log
  server, per the specification, and the router identity comes from the
  UDP/TCP source address -- not from anything inside the message, which is
  trivially spoofable.
"""

from __future__ import annotations

import re

OCTET = r"(?:25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])"
IPV4 = rf"(?:{OCTET}\.){{3}}{OCTET}"
PORT = r"\d{1,5}"
NOT_ADDR_AFTER = r"(?![\d.:])"      # address must not run into another address/port
NOT_ADDR_BEFORE = r"(?<![\d.])"

# --- source NAT with ports (TCP/UDP/SCTP) --------------------------------
# ... NAT (100.68.180.230:35420->103.125.177.119:35420)->99.124.164.160:22
SRCNAT_PORTS = re.compile(
    rf"NAT\s*\((?P<pri>{IPV4}):(?P<pri_port>{PORT})"
    rf"->(?P<pub>{IPV4}):(?P<pub_port>{PORT})\)"
    rf"->(?P<dst>{IPV4}):(?P<dst_port>{PORT})"
)

# --- source NAT without ports (ICMP, GRE, ESP, raw protocols) -------------
# ... NAT (10.20.0.5->103.73.100.12)->8.8.8.8
SRCNAT_NOPORTS = re.compile(
    rf"NAT\s*\((?P<pri>{IPV4})->(?P<pub>{IPV4})\)->(?P<dst>{IPV4}){NOT_ADDR_AFTER}"
)

# --- no NAT applied: plain forward/input logging with ports ---------------
# ... proto TCP (SYN), 10.20.0.5:1234->8.8.8.8:443, len 60
PLAIN_PORTS = re.compile(
    rf"{NOT_ADDR_BEFORE}(?P<pri>{IPV4}):(?P<pri_port>{PORT})"
    rf"->(?P<dst>{IPV4}):(?P<dst_port>{PORT})"
)

# --- no NAT applied, no ports --------------------------------------------
PLAIN_NOPORTS = re.compile(
    rf"{NOT_ADDR_BEFORE}(?P<pri>{IPV4})->(?P<dst>{IPV4}){NOT_ADDR_AFTER}"
)

# Fallback for interfaces written without angle brackets (in:ether1).
# The common `in:<name>` form is handled by a str.find() fast path.
IN_INTERFACE = re.compile(r"\bin:<?([^\s,>]*)>?")

# Fallback for the protocol field; the common case is handled by str.find().
PROTOCOL = re.compile(r"\bproto\s+([A-Za-z0-9_-]{1,16})")

# PPPoE interfaces are named "<pppoe-USERNAME>" by RouterOS. Some operators
# terminate l2tp/pptp/sstp/ovpn sessions on the same BRAS.
SUBSCRIBER_PREFIXES = ("pppoe-in-", "pppoe-out-", "pppoe-", "l2tp-", "pptp-", "sstp-", "ovpn-")

# Numeric IP protocol numbers are translated to names so the LowCardinality
# dictionary in ClickHouse does not split "6" and "TCP" into two values.
PROTO_NUMBERS = {
    "1": "ICMP", "2": "IGMP", "6": "TCP", "17": "UDP", "41": "IPV6",
    "47": "GRE", "50": "ESP", "51": "AH", "58": "ICMPV6", "89": "OSPF",
    "132": "SCTP",
}
