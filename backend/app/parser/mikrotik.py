"""MikroTik firewall/NAT syslog parser.

Hot path. Every microsecond here costs throughput at 100k logs/sec, so:

* the payload is probed with `str.find()` before any regex runs -- a
  non-matching line costs one substring search, not a backtracking regex;
* IPv4 octet ranges are validated by the regex engine, not by Python;
* the protocol and interface fields are cut out with `find()`/slicing and
  only fall back to a regex for the uncommon spellings;
* the result is a slotted dataclass, not a dict.

Malformed input never raises. Callers get `(None, reason)` and bump a counter.
"""

from __future__ import annotations

from typing import Optional, Tuple

from . import patterns as P
from .base import DECODE, NO_MATCH, NO_NAT, ParsedLog

_PROTO_END = frozenset(" ,()\t\r\n")


class MikroTikParser:
    """Parse RouterOS firewall log payloads into normalised records.

    Parameters
    ----------
    require_nat:
        Drop events that carry no NAT translation. ISP CGNAT deployments want
        this on: a forward-chain log without a translation cannot answer the
        question this database exists to answer. Turn it off to also capture
        plain firewall logs, in which case public_ip equals private_ip.
    subscriber_from_interface:
        When the inbound interface is not a PPPoE/tunnel session (a bridge or
        VLAN, say), store the raw interface name as the subscriber id instead
        of leaving it empty.
    """

    __slots__ = ("require_nat", "subscriber_from_interface")

    def __init__(self, require_nat: bool = True, subscriber_from_interface: bool = False):
        self.require_nat = require_nat
        self.subscriber_from_interface = subscriber_from_interface

    # -- public API --------------------------------------------------------
    def parse(self, payload: str) -> Tuple[Optional[ParsedLog], str]:
        try:
            return self._parse(payload)
        except Exception:  # pragma: no cover - the parser must never kill the receiver
            return None, DECODE

    def parse_bytes(self, payload: bytes) -> Tuple[Optional[ParsedLog], str]:
        try:
            text = payload.decode("utf-8", "replace")
        except Exception:  # pragma: no cover
            return None, DECODE
        return self.parse(text)

    # -- internals ---------------------------------------------------------
    def _parse(self, payload: str) -> Tuple[Optional[ParsedLog], str]:
        if payload.find("NAT") >= 0:
            m = P.SRCNAT_PORTS.search(payload)
            if m is not None:
                g = m.group
                pri, pri_port = g("pri"), g("pri_port")
                pub, pub_port = g("pub"), g("pub_port")
                dst, dst_port = g("dst"), g("dst_port")
            else:
                m = P.SRCNAT_NOPORTS.search(payload)
                if m is None:
                    return None, NO_MATCH
                g = m.group
                pri, pub, dst = g("pri"), g("pub"), g("dst")
                pri_port = pub_port = dst_port = "0"
        else:
            if self.require_nat:
                return None, NO_NAT
            m = P.PLAIN_PORTS.search(payload)
            if m is not None:
                g = m.group
                pri, pri_port = g("pri"), g("pri_port")
                dst, dst_port = g("dst"), g("dst_port")
                pub, pub_port = pri, pri_port
            else:
                m = P.PLAIN_NOPORTS.search(payload)
                if m is None:
                    return None, NO_MATCH
                pri, dst = m.group("pri"), m.group("dst")
                pub = pri
                pri_port = pub_port = dst_port = "0"

        pri_p = int(pri_port)
        pub_p = int(pub_port)
        dst_p = int(dst_port)
        if pri_p > 65535 or pub_p > 65535 or dst_p > 65535:
            return None, NO_MATCH

        return ParsedLog(
            private_ip=pri,
            private_port=pri_p,
            public_ip=pub,
            public_port=pub_p,
            dest_ip=dst,
            dest_port=dst_p,
            protocol=_protocol(payload),
            subscriber_id=self._subscriber(payload),
        ), ""

    def _subscriber(self, payload: str) -> str:
        i = payload.find("in:<")
        if i >= 0:
            j = payload.find(">", i + 4)
            iface = payload[i + 4:j] if j > 0 else ""
        else:
            m = P.IN_INTERFACE.search(payload)
            if m is None:
                return ""
            iface = m.group(1)
        if not iface:
            return ""
        for prefix in P.SUBSCRIBER_PREFIXES:
            if iface.startswith(prefix):
                return iface[len(prefix):]
        return iface if self.subscriber_from_interface else ""


def _protocol(payload: str) -> str:
    i = payload.find("proto ")
    if i < 0:
        m = P.PROTOCOL.search(payload)
        if m is None:
            return ""
        raw = m.group(1)
    else:
        j = i + 6
        k = j
        n = len(payload)
        while k < n and payload[k] not in _PROTO_END:
            k += 1
        raw = payload[j:k]
        if not raw:
            return ""
    if raw.isdigit():
        return P.PROTO_NUMBERS.get(raw, raw)
    return raw.upper()


__all__ = ["MikroTikParser", "ParsedLog"]
