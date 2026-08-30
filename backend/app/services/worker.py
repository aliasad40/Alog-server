"""Redis -> ClickHouse batch writer.

Behaviour when ClickHouse is unavailable is the reason this service exists.
The worker claims a batch, tries to insert it, and on failure keeps retrying
with exponential backoff *without ever discarding the claim*. Records stay in
the in-flight list until the insert is acknowledged. When ClickHouse comes
back, the backlog drains at full batch size.

The insert itself is blocking (clickhouse-connect is sync), so it runs in a
thread; the event loop stays free to claim the next batch and answer signals.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import socket
import time
from datetime import datetime
from typing import List, Optional

from ..config import Config, get_config
from ..database.clickhouse import ClickHouseService
from ..logging_setup import setup_logging
from ..metrics import (
    CH_INSERT_BATCHES, CH_INSERT_ERRORS, PARSER_ERRORS, STORED, Metrics,
)
from ..queue.redis_queue import LogQueue, build_client, wait_for_redis

log = logging.getLogger(__name__)

INFLIGHT_PREFIX = "nls:inflight:"
MAX_BACKOFF = 30.0


class BatchWorker:
    def __init__(self, cfg: Config, worker_id: str):
        self.cfg = cfg
        self.worker_id = worker_id
        self.inflight_key = f"{INFLIGHT_PREFIX}{worker_id}"
        self.redis = build_client(cfg)
        self.queue = LogQueue(self.redis, cfg)
        self.metrics = Metrics(self.redis)
        self.ch = ClickHouseService(cfg)
        self._stop = asyncio.Event()

    async def run(self) -> None:
        if not await wait_for_redis(self.redis):
            raise SystemExit("redis unavailable; refusing to start worker")
        self.metrics.start()

        # Anything left in *our* in-flight list is from a previous crash.
        recovered = await self.queue.release(self.inflight_key)
        if recovered:
            log.warning("requeued %d records left over from a previous run", recovered)
        # Claim keys from receivers/workers that no longer exist are handled
        # by the maintenance service, not here, to avoid two workers fighting
        # over the same list.

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self._stop.set)

        log.info("worker %s ready (batch=%d, interval=%dms)",
                 self.worker_id, self.cfg.clickhouse.batch_size,
                 self.cfg.clickhouse.batch_interval_ms)

        idle_sleep = self.cfg.clickhouse.batch_interval_ms / 1000.0
        while not self._stop.is_set():
            try:
                claimed = await self.queue.claim(self.inflight_key, self.cfg.clickhouse.batch_size)
            except Exception as exc:
                log.error("could not claim from redis: %s", exc)
                await self._sleep(2.0)
                continue

            if not claimed:
                await self._sleep(idle_sleep)
                continue

            await self._write_with_retry(claimed)

        await self.shutdown()

    async def _write_with_retry(self, claimed: List[bytes]) -> None:
        rows, bad = self._decode(claimed)
        if bad:
            self.metrics.incr(PARSER_ERRORS, bad)
        if not rows:
            await self.queue.ack(self.inflight_key)
            return

        backoff = 1.0
        attempt = 0
        while not self._stop.is_set():
            attempt += 1
            started = time.monotonic()
            try:
                await asyncio.to_thread(self.ch.insert_rows, rows)
            except Exception as exc:
                self.metrics.incr(CH_INSERT_ERRORS)
                log.error("clickhouse insert failed (attempt %d, %d rows): %s",
                          attempt, len(rows), exc)
                # Drop the pooled connection; a half-open socket would fail
                # every subsequent attempt for the same reason.
                self.ch.close()
                await self._sleep(backoff)
                backoff = min(backoff * 2, MAX_BACKOFF)
                continue

            elapsed = (time.monotonic() - started) * 1000
            await self.queue.ack(self.inflight_key)
            self.metrics.incr(STORED, len(rows))
            self.metrics.incr(CH_INSERT_BATCHES)
            await self.metrics.set_gauge("last_insert_ms", round(elapsed, 1))
            await self.metrics.set_gauge("last_insert_at", int(time.time()))
            log.debug("inserted %d rows in %.0f ms", len(rows), elapsed)
            return

        # Shutting down mid-retry: hand the claim back so the next worker
        # picks it up instead of leaving it orphaned.
        await self.queue.release(self.inflight_key)

    @staticmethod
    def _decode(claimed: List[bytes]):
        rows, bad = [], 0
        for payload in claimed:
            row = LogQueue.decode(payload)
            if row is None:
                bad += 1
                continue
            # msgpack gave us an epoch int; ClickHouse wants a datetime.
            row[0] = datetime.fromtimestamp(row[0])
            rows.append(row)
        return rows, bad

    async def _sleep(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    async def shutdown(self) -> None:
        log.info("worker draining")
        try:
            await self.queue.release(self.inflight_key)
        except Exception:
            pass
        await self.metrics.stop()
        self.ch.close()
        await self.redis.aclose()
        log.info("worker stopped")


def main() -> None:
    cfg = get_config()
    setup_logging(cfg, "worker")
    try:
        import uvloop
        uvloop.install()
    except ImportError:  # pragma: no cover
        pass
    worker_id = os.environ.get("NLS_WORKER_ID") or f"{socket.gethostname()}-{os.getpid()}"
    worker = BatchWorker(cfg, worker_id)
    try:
        asyncio.run(worker.run())
    except KeyboardInterrupt:  # pragma: no cover
        pass


if __name__ == "__main__":
    main()
