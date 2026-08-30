"""End-to-end ingest test against a real Redis.

Covers the path the specification cares most about: an authorised router's
log survives ClickHouse being down, and an unauthorised router's log never
reaches the queue at all.

Requires a Redis on 127.0.0.1:6399. Skipped otherwise.
    redis-server --port 6399 --save '' --appendonly no --daemonize yes
"""

from __future__ import annotations

import asyncio
import os
import socket
import sys
from datetime import datetime

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "backend"))

from app.config import Config
from app.metrics import DROPPED_UNKNOWN_ROUTER, PARSED, PARSER_ERRORS, Metrics
from app.parser import MikroTikParser
from app.queue.redis_queue import LogQueue, build_client
from app.services.receiver import Ingest, RouterRegistry

REDIS_PORT = 6399
AUTHORISED = "10.10.10.1"
UNAUTHORISED = "10.10.10.99"

SAMPLE = (b"<134>Aug 30 18:33:17 BRAS-01 firewall,info forward: in:<pppoe-P2-musa> "
          b"out:vlan2436, connection-state:new,snat proto TCP (SYN), "
          b"100.68.180.230:35420->99.124.164.160:22, "
          b"NAT (100.68.180.230:35420->103.125.177.119:35420)->99.124.164.160:22, len 60")


def redis_available() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", REDIS_PORT), timeout=1):
            return True
    except OSError:
        return False


pytestmark = pytest.mark.skipif(not redis_available(), reason="no Redis on 127.0.0.1:6399")


def make_config(tmp_path) -> Config:
    return Config(
        secret_key="x" * 48,
        redis={"port": REDIS_PORT, "queue_key": "nls:test:queue", "max_queue_length": 1000},
        paths={
            "data_dir": str(tmp_path),
            "log_dir": str(tmp_path),
            "metadata_db": str(tmp_path / "meta.sqlite3"),
            "logo_dir": str(tmp_path / "branding"),
        },
    )


class FakeRegistry:
    def __init__(self, ips):
        self.ips = set(ips)

    def __contains__(self, ip):
        return ip in self.ips


@pytest.mark.asyncio
async def test_authorised_log_reaches_the_queue_and_decodes(tmp_path):
    cfg = make_config(tmp_path)
    client = build_client(cfg)
    await client.delete(cfg.redis.queue_key)
    queue = LogQueue(client, cfg)
    metrics = Metrics(None)
    ingest = Ingest(cfg, queue, FakeRegistry([AUTHORISED]), metrics)

    ingest.handle(SAMPLE, AUTHORISED)
    await ingest._push()

    assert await queue.length() == 1
    claimed = await queue.claim("nls:test:inflight", 10)
    row = LogQueue.decode(claimed[0])

    assert row[1] == AUTHORISED          # router_ip is the packet source
    assert row[2] == "P2-musa"           # subscriber_id
    assert row[3] == "100.68.180.230"    # private_ip
    assert row[5] == "103.125.177.119"   # public_ip
    assert row[7] == "99.124.164.160"    # dest_ip -- not reversed
    assert row[8] == 22
    assert row[9] == "TCP"
    assert row[10] is None               # session_end_time
    assert metrics._counts[PARSED] == 1

    await queue.ack("nls:test:inflight")
    await client.aclose()


@pytest.mark.asyncio
async def test_unauthorised_router_never_reaches_the_queue(tmp_path):
    cfg = make_config(tmp_path)
    client = build_client(cfg)
    await client.delete(cfg.redis.queue_key)
    queue = LogQueue(client, cfg)
    metrics = Metrics(None)
    ingest = Ingest(cfg, queue, FakeRegistry([AUTHORISED]), metrics)

    for _ in range(50):
        ingest.handle(SAMPLE, UNAUTHORISED)
    await ingest._push()

    assert await queue.length() == 0
    assert metrics._counts[DROPPED_UNKNOWN_ROUTER] == 50
    assert PARSED not in metrics._counts   # parser never ran
    await client.aclose()


@pytest.mark.asyncio
async def test_claim_is_recoverable_when_the_consumer_dies(tmp_path):
    """The core log-loss guarantee: a claimed batch that is never acked
    comes back to the queue intact and in order."""
    cfg = make_config(tmp_path)
    client = build_client(cfg)
    await client.delete(cfg.redis.queue_key, "nls:test:inflight")
    queue = LogQueue(client, cfg)

    payloads = [LogQueue.encode([i, "10.0.0.1", "s", "10.0.0.2", 1,
                                 "1.2.3.4", 2, "8.8.8.8", 53, "UDP", None])
                for i in range(2500)]
    await queue.push_many(payloads)

    claimed = await queue.claim("nls:test:inflight", 2500)
    assert len(claimed) == 2500
    assert await queue.length() == 0          # removed from the main queue...

    # ...but not lost: simulate the worker being killed before it acked.
    recovered = await queue.release("nls:test:inflight")
    assert recovered == 2500
    assert await queue.length() == 2500

    back = await queue.claim("nls:test:inflight", 2500)
    assert [LogQueue.decode(p)[0] for p in back] == list(range(2500))  # order preserved
    await queue.ack("nls:test:inflight")
    await client.aclose()


@pytest.mark.asyncio
async def test_orphan_recovery_finds_abandoned_claims(tmp_path):
    cfg = make_config(tmp_path)
    client = build_client(cfg)
    await client.delete(cfg.redis.queue_key)
    queue = LogQueue(client, cfg)

    await client.rpush("nls:test:orphan:dead-1", b"a", b"b", b"c")
    recovered = await queue.recover_orphans("nls:test:orphan:")
    assert recovered == 3
    assert await queue.length() == 3
    await client.delete(cfg.redis.queue_key)
    await client.aclose()


@pytest.mark.asyncio
async def test_backpressure_sheds_load_instead_of_filling_redis(tmp_path):
    cfg = make_config(tmp_path)
    cfg.redis.max_queue_length = 100
    client = build_client(cfg)
    await client.delete(cfg.redis.queue_key)
    queue = LogQueue(client, cfg)
    metrics = Metrics(None)
    ingest = Ingest(cfg, queue, FakeRegistry([AUTHORISED]), metrics)

    for _ in range(150):
        ingest.handle(SAMPLE, AUTHORISED)
    await ingest._push()
    await ingest._check_backpressure()

    ingest.handle(SAMPLE, AUTHORISED)
    await ingest._push()
    assert metrics._counts.get("logs_dropped_backpressure", 0) > 0

    await client.delete(cfg.redis.queue_key)
    await client.aclose()


@pytest.mark.asyncio
async def test_garbage_traffic_is_counted_not_queued(tmp_path):
    cfg = make_config(tmp_path)
    client = build_client(cfg)
    await client.delete(cfg.redis.queue_key)
    queue = LogQueue(client, cfg)
    metrics = Metrics(None)
    ingest = Ingest(cfg, queue, FakeRegistry([AUTHORISED]), metrics)

    for payload in (b"", b"\xff\xfe garbage", b"<13>Aug 30 sshd: login", b"x" * 3000):
        ingest.handle(payload, AUTHORISED)
    await ingest._push()

    assert await queue.length() == 0
    assert metrics._counts[PARSER_ERRORS] == 4
    await client.aclose()


@pytest.mark.asyncio
async def test_udp_socket_path_receives_real_datagrams(tmp_path):
    """Bind the real protocol on a high port and send actual UDP packets."""
    from app.services.receiver import SyslogUDP

    cfg = make_config(tmp_path)
    cfg.receiver.udp_port = 15514
    client = build_client(cfg)
    await client.delete(cfg.redis.queue_key)
    queue = LogQueue(client, cfg)
    ingest = Ingest(cfg, queue, FakeRegistry(["127.0.0.1"]), Metrics(None))

    loop = asyncio.get_running_loop()
    transport, _ = await loop.create_datagram_endpoint(
        lambda: SyslogUDP(ingest), local_addr=("127.0.0.1", 15514)
    )
    try:
        sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        for _ in range(20):
            sender.sendto(SAMPLE, ("127.0.0.1", 15514))
        sender.close()
        await asyncio.sleep(0.3)
        await ingest._push()
        assert await queue.length() == 20
    finally:
        transport.close()
        await client.delete(cfg.redis.queue_key)
        await client.aclose()
