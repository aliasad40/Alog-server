"""Shared types for the log parser.

The parser is deliberately independent of Redis, ClickHouse and the web
application: it takes bytes/str in and returns a `ParsedLog` or `None`.
That makes it trivially unit-testable and replaceable.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final


class ParseError(str):
    """Reason codes for dropped logs (used as metric labels)."""


# Reason codes -------------------------------------------------------------
NO_MATCH: Final = "no_match"
BAD_IP: Final = "bad_ip"
BAD_PORT: Final = "bad_port"
NO_NAT: Final = "no_nat"
DECODE: Final = "decode"


@dataclass(slots=True)
class ParsedLog:
    """One normalised NAT/firewall event.

    Field order matches the ClickHouse column order and the msgpack wire
    format used on the Redis queue. Do not reorder without bumping
    `queue.WIRE_VERSION`.
    """

    private_ip: str
    private_port: int
    public_ip: str
    public_port: int
    dest_ip: str
    dest_port: int
    protocol: str
    subscriber_id: str

    def as_row(self, ts: int, router_ip: str) -> list:
        return [
            ts,
            router_ip,
            self.subscriber_id,
            self.private_ip,
            self.private_port,
            self.public_ip,
            self.public_port,
            self.dest_ip,
            self.dest_port,
            self.protocol,
            None,  # session_end_time
        ]


# --- fast validation ------------------------------------------------------
# The regexes only guarantee "1-3 digits", so octet ranges are checked here.
# Doing it by hand is ~4x faster than ipaddress.IPv4Address() and avoids
# allocating an object per field per log line.

def valid_ipv4(text: str) -> bool:
    n = 0
    octet = -1
    for ch in text:
        if ch == ".":
            if octet < 0 or octet > 255:
                return False
            n += 1
            octet = -1
        else:
            d = ord(ch) - 48
            if d < 0 or d > 9:
                return False
            octet = d if octet < 0 else octet * 10 + d
            if octet > 255:
                return False
    return n == 3 and 0 <= octet <= 255


def valid_port(value: int) -> bool:
    return 0 <= value <= 65535
