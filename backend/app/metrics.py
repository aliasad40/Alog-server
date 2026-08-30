"""Counters shared across processes.

Incrementing Redis once per log would double our Redis traffic, so each
process keeps counters in a local dict and flushes the deltas on a timer.
Worst case on an unclean kill we lose one flush interval of statistics --
acceptable for metrics, which is why the log data itself does not work
this way.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Dict, Optional

import redis.asyncio as aioredis

log = logging.getLogger(__name__)

METRICS_KEY = "nls:metrics"
GAUGE_KEY = "nls:gauges"

# Monotonic counters
RECEIVED = "logs_received"
PARSED = "logs_parsed"
STORED = "logs_stored"
DROPPED_UNKNOWN_ROUTER = "logs_unknown_router"
DROPPED_BACKPRESSURE = "logs_dropped_backpressure"
DROPPED_REDIS_ERROR = "logs_dropped_redis_error"
PARSER_ERRORS = "parser_errors"
CH_INSERT_ERRORS = "clickhouse_insert_errors"
CH_INSERT_BATCHES = "clickhouse_insert_batches"
BYTES_RECEIVED = "bytes_received"

ALL_COUNTERS = [
    RECEIVED, PARSED, STORED, DROPPED_UNKNOWN_ROUTER, DROPPED_BACKPRESSURE,
    DROPPED_REDIS_ERROR, PARSER_ERRORS, CH_INSERT_ERRORS, CH_INSERT_BATCHES,
    BYTES_RECEIVED,
]


class Metrics:
    """Local counter buffer with a background flush to Redis."""

    __slots__ = ("_counts", "_redis", "_task", "_interval", "_stopping")

    def __init__(self, client: Optional[aioredis.Redis] = None, interval: float = 2.0):
        self._counts: Dict[str, int] = {}
        self._redis = client
        self._task: Optional[asyncio.Task] = None
        self._interval = interval
        self._stopping = False

    def incr(self, name: str, value: int = 1) -> None:
        self._counts[name] = self._counts.get(name, 0) + value

    def start(self) -> None:
        if self._redis is not None and self._task is None:
            self._task = asyncio.create_task(self._loop(), name="metrics-flush")

    async def stop(self) -> None:
        self._stopping = True
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        await self.flush()

    async def flush(self) -> None:
        if self._redis is None or not self._counts:
            return
        pending, self._counts = self._counts, {}
        try:
            pipe = self._redis.pipeline(transaction=False)
            for name, value in pending.items():
                pipe.hincrby(METRICS_KEY, name, value)
            await pipe.execute()
        except Exception as exc:
            # Fold the deltas back so nothing is lost if Redis recovers.
            for name, value in pending.items():
                self._counts[name] = self._counts.get(name, 0) + value
            log.debug("metrics flush failed: %s", exc)

    async def set_gauge(self, name: str, value: float) -> None:
        if self._redis is None:
            return
        try:
            await self._redis.hset(GAUGE_KEY, name, str(value))
        except Exception:
            pass

    async def _loop(self) -> None:
        while not self._stopping:
            await asyncio.sleep(self._interval)
            await self.flush()


async def read_all(client: aioredis.Redis) -> Dict[str, int]:
    try:
        raw = await client.hgetall(METRICS_KEY)
    except Exception:
        return {name: 0 for name in ALL_COUNTERS}
    out = {name: 0 for name in ALL_COUNTERS}
    for key, value in raw.items():
        name = key.decode() if isinstance(key, bytes) else key
        try:
            out[name] = int(value)
        except (TypeError, ValueError):
            continue
    return out


async def read_gauges(client: aioredis.Redis) -> Dict[str, float]:
    try:
        raw = await client.hgetall(GAUGE_KEY)
    except Exception:
        return {}
    out = {}
    for key, value in raw.items():
        name = key.decode() if isinstance(key, bytes) else key
        try:
            out[name] = float(value)
        except (TypeError, ValueError):
            continue
    return out


class RateTracker:
    """Derives a per-second rate from a monotonic counter between polls."""

    def __init__(self):
        self._last: Dict[str, tuple] = {}

    def rate(self, name: str, value: int) -> float:
        now = time.monotonic()
        prev = self._last.get(name)
        self._last[name] = (now, value)
        if prev is None:
            return 0.0
        prev_t, prev_v = prev
        dt = now - prev_t
        if dt <= 0 or value < prev_v:
            return 0.0
        return round((value - prev_v) / dt, 1)
