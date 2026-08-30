"""Syslog receiver: UDP + TCP, source-IP authorisation, parse, enqueue.

Scaling model
-------------
CPython cannot saturate a 10G syslog feed on one core, so the receiver runs
as N processes that each bind the same UDP port with SO_REUSEPORT. The kernel
hashes each datagram to one socket, so the processes never contend and there
is no user-space fan-out. Adding cores adds throughput almost linearly.

Authorisation
-------------
The authorised router set lives in a plain Python `set` in each process,
refreshed from Redis on a timer. Checking it is a dict lookup on the hot path:
no database round trip, no lock. An unauthorised datagram is dropped after one
hash lookup and never reaches the parser, which is the point of section 14 --
an attacker spraying the syslog port costs us a set lookup, not a regex.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import socket
import time
from typing import List, Optional, Set

from ..config import Config, get_config
from ..database.meta import MetadataStore
from ..logging_setup import setup_logging
from ..metrics import (
    BYTES_RECEIVED, DROPPED_BACKPRESSURE, DROPPED_REDIS_ERROR,
    DROPPED_UNKNOWN_ROUTER, PARSED, PARSER_ERRORS, RECEIVED, Metrics,
)
from ..parser import MikroTikParser
from ..queue.redis_queue import LogQueue, build_client, wait_for_redis

log = logging.getLogger(__name__)

ROUTER_SET_KEY = "nls:authorized_routers"
ROUTER_RELOAD_CHANNEL = "nls:routers:changed"


class RouterRegistry:
    """In-memory allow-list of router source addresses."""

    __slots__ = ("_ips", "_redis", "_meta", "_interval", "_task", "_stopping")

    def __init__(self, redis_client, meta: Optional[MetadataStore], interval: int):
        self._ips: Set[str] = set()
        self._redis = redis_client
        self._meta = meta
        self._interval = interval
        self._task: Optional[asyncio.Task] = None
        self._stopping = False

    def __contains__(self, ip: str) -> bool:
        return ip in self._ips

    @property
    def size(self) -> int:
        return len(self._ips)

    async def refresh(self) -> None:
        ips: Optional[Set[str]] = None
        try:
            members = await self._redis.smembers(ROUTER_SET_KEY)
            ips = {m.decode() if isinstance(m, bytes) else m for m in members}
        except Exception as exc:
            log.warning("router set unavailable in Redis: %s", exc)
        # Redis holds a cache of the SQLite table. If the cache is empty or
        # unreachable we read the source of truth directly rather than
        # silently dropping every log on the floor.
        if not ips and self._meta is not None:
            try:
                ips = set(self._meta.enabled_router_ips())
                await self._publish(ips)
            except Exception as exc:
                log.error("cannot load routers from metadata store: %s", exc)
                return
        if ips is not None and ips != self._ips:
            log.info("authorised routers: %d -> %d", len(self._ips), len(ips))
            self._ips = ips

    async def _publish(self, ips: Set[str]) -> None:
        try:
            pipe = self._redis.pipeline(transaction=True)
            pipe.delete(ROUTER_SET_KEY)
            if ips:
                pipe.sadd(ROUTER_SET_KEY, *ips)
            await pipe.execute()
        except Exception:
            pass

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="router-refresh")

    async def stop(self) -> None:
        self._stopping = True
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _loop(self) -> None:
        while not self._stopping:
            await asyncio.sleep(self._interval)
            try:
                await self.refresh()
            except Exception as exc:  # pragma: no cover
                log.warning("router refresh failed: %s", exc)


class Ingest:
    """Parse -> encode -> buffer -> pipelined push to Redis."""

    def __init__(self, cfg: Config, queue: LogQueue, registry: RouterRegistry, metrics: Metrics):
        self.cfg = cfg
        self.queue = queue
        self.registry = registry
        self.metrics = metrics
        self.parser = MikroTikParser(
            require_nat=cfg.parser.require_nat,
            subscriber_from_interface=cfg.parser.subscriber_from_interface,
        )
        self._buffer: List[bytes] = []
        self._batch = cfg.receiver.push_batch
        self._flush_task: Optional[asyncio.Task] = None
        self._pushing = False
        self._paused_until = 0.0
        self._last_warn = 0.0

    def handle(self, payload: bytes, source_ip: str) -> None:
        """Hot path. Must not await and must not raise."""
        m = self.metrics
        m.incr(RECEIVED)
        m.incr(BYTES_RECEIVED, len(payload))

        if source_ip not in self.registry:
            m.incr(DROPPED_UNKNOWN_ROUTER)
            return

        record, reason = self.parser.parse_bytes(payload)
        if record is None:
            m.incr(PARSER_ERRORS)
            return
        m.incr(PARSED)

        self._buffer.append(
            LogQueue.encode(record.as_row(int(time.time()), source_ip))
        )
        if len(self._buffer) >= self._batch:
            self._schedule_flush()

    def _schedule_flush(self) -> None:
        if not self._pushing and self._buffer:
            self._pushing = True
            asyncio.get_running_loop().create_task(self._push())

    async def _push(self) -> None:
        try:
            batch, self._buffer = self._buffer, []
            if not batch:
                return
            now = time.monotonic()
            if now < self._paused_until:
                self.metrics.incr(DROPPED_BACKPRESSURE, len(batch))
                return
            try:
                await self.queue.push_many(batch)
            except Exception as exc:
                self.metrics.incr(DROPPED_REDIS_ERROR, len(batch))
                if now - self._last_warn > 10:
                    self._last_warn = now
                    log.error("redis push failed, dropping %d records: %s", len(batch), exc)
        finally:
            self._pushing = False

    async def run_flusher(self) -> None:
        """Time-based flush so a trickle of logs still lands promptly, plus
        a periodic backpressure check against the queue depth."""
        interval = self.cfg.receiver.push_interval_ms / 1000.0
        check_every = max(1, int(2.0 / interval))
        tick = 0
        while True:
            await asyncio.sleep(interval)
            self._schedule_flush()
            tick += 1
            if tick % check_every == 0:
                await self._check_backpressure()

    async def _check_backpressure(self) -> None:
        depth = await self.queue.length()
        if depth < 0:
            return
        await self.metrics.set_gauge("redis_queue_length", depth)
        if depth > self.cfg.redis.max_queue_length:
            self._paused_until = time.monotonic() + 5.0
            log.error(
                "queue depth %d exceeds max_queue_length %d -- shedding load for 5s. "
                "ClickHouse is probably down or too slow.",
                depth, self.cfg.redis.max_queue_length,
            )


class SyslogUDP(asyncio.DatagramProtocol):
    def __init__(self, ingest: Ingest):
        self.ingest = ingest

    def datagram_received(self, data: bytes, addr) -> None:
        self.ingest.handle(data, addr[0])

    def error_received(self, exc) -> None:  # pragma: no cover
        log.debug("udp error: %s", exc)


class SyslogTCP(asyncio.Protocol):
    """RFC 6587 non-transparent framing (newline delimited).

    Octet-counted framing ("123 <134>...") is also accepted because RouterOS
    can be configured either way.
    """

    MAX_LINE = 64 * 1024

    def __init__(self, ingest: Ingest):
        self.ingest = ingest
        self._buf = bytearray()
        self._peer = ""

    def connection_made(self, transport) -> None:
        peer = transport.get_extra_info("peername")
        self._peer = peer[0] if peer else ""
        if self._peer not in self.ingest.registry:
            # Reject unauthorised senders at connect time, not per line.
            transport.abort()

    def data_received(self, data: bytes) -> None:
        self._buf.extend(data)
        if len(self._buf) > self.MAX_LINE:
            # A sender with no newlines would otherwise grow this forever.
            del self._buf[:-self.MAX_LINE]
        while True:
            idx = self._buf.find(b"\n")
            if idx < 0:
                break
            line = bytes(self._buf[:idx]).rstrip(b"\r")
            del self._buf[:idx + 1]
            if line:
                self.ingest.handle(_strip_octet_count(line), self._peer)


def _strip_octet_count(line: bytes) -> bytes:
    """Turn '129 <134>Aug 30 ...' into '<134>Aug 30 ...'."""
    sp = line.find(b" ")
    if 0 < sp <= 6 and line[:sp].isdigit():
        return line[sp + 1:]
    return line


def _udp_socket(cfg: Config) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if hasattr(socket, "SO_REUSEPORT"):
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, cfg.receiver.so_rcvbuf)
    except OSError:
        log.warning("could not set SO_RCVBUF=%d; raise net.core.rmem_max",
                    cfg.receiver.so_rcvbuf)
    sock.setblocking(False)
    sock.bind((cfg.receiver.bind_address, cfg.receiver.udp_port))
    actual = sock.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF)
    log.info("udp %s:%d bound, rcvbuf=%d bytes",
             cfg.receiver.bind_address, cfg.receiver.udp_port, actual)
    return sock


async def _serve(cfg: Config) -> None:
    loop = asyncio.get_running_loop()
    redis_client = build_client(cfg)
    if not await wait_for_redis(redis_client):
        raise SystemExit("redis unavailable; refusing to start receiver")

    metrics = Metrics(redis_client)
    metrics.start()
    queue = LogQueue(redis_client, cfg)
    meta = MetadataStore(cfg.paths.metadata_db)
    registry = RouterRegistry(redis_client, meta, cfg.receiver.router_refresh_seconds)
    await registry.refresh()
    registry.start()
    if registry.size == 0:
        log.warning("no authorised routers configured -- all logs will be dropped. "
                    "Add routers in the web interface.")

    ingest = Ingest(cfg, queue, registry, metrics)
    flusher = asyncio.create_task(ingest.run_flusher(), name="ingest-flush")

    transports = []
    if cfg.receiver.udp_enabled:
        transport, _ = await loop.create_datagram_endpoint(
            lambda: SyslogUDP(ingest), sock=_udp_socket(cfg)
        )
        transports.append(transport)

    server = None
    if cfg.receiver.tcp_enabled:
        server = await loop.create_server(
            lambda: SyslogTCP(ingest),
            host=cfg.receiver.bind_address,
            port=cfg.receiver.tcp_port,
            reuse_address=True,
            reuse_port=hasattr(socket, "SO_REUSEPORT"),
            backlog=512,
        )
        log.info("tcp %s:%d listening", cfg.receiver.bind_address, cfg.receiver.tcp_port)

    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    log.info("receiver ready (pid %d)", os.getpid())
    await stop.wait()

    log.info("receiver draining")
    flusher.cancel()
    for transport in transports:
        transport.close()
    if server:
        server.close()
        await server.wait_closed()
    await ingest._push()          # flush whatever is buffered before exiting
    await registry.stop()
    await metrics.stop()
    await redis_client.aclose()
    log.info("receiver stopped")


def main() -> None:
    cfg = get_config()
    setup_logging(cfg, "receiver")

    workers = cfg.receiver.workers or max(1, (os.cpu_count() or 2) // 2)
    if workers > 1 and hasattr(socket, "SO_REUSEPORT"):
        children = []
        for _ in range(workers - 1):
            pid = os.fork()
            if pid == 0:
                _run_once(cfg)
                os._exit(0)
            children.append(pid)
        log.info("started %d receiver processes", workers)
        try:
            _run_once(cfg)
        finally:
            for pid in children:
                try:
                    os.kill(pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            for pid in children:
                try:
                    os.waitpid(pid, 0)
                except ChildProcessError:
                    pass
    else:
        _run_once(cfg)


def _run_once(cfg: Config) -> None:
    try:
        import uvloop
        uvloop.install()
    except ImportError:  # pragma: no cover
        log.info("uvloop not available, using the default event loop")
    try:
        asyncio.run(_serve(cfg))
    except KeyboardInterrupt:  # pragma: no cover
        pass


if __name__ == "__main__":
    main()
