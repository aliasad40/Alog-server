"""Redis buffer between the receiver and ClickHouse.

Reliability model
-----------------
A plain LPOP loses data: if the worker pops 20 000 rows and then ClickHouse
refuses the insert, those rows are gone. So the worker *claims* a batch
instead. A Lua script atomically moves N entries from the shared queue into a
per-worker in-flight list; the entries are only deleted once ClickHouse has
acknowledged the insert. If the worker is killed mid-batch, the entries are
still sitting in its in-flight list and are recovered on next start.

That gives at-least-once delivery. Duplicates are possible in exactly one
window (insert succeeded, ack didn't), which for append-only NAT logs is the
right trade -- a duplicate row is harmless, a missing one is a failed lawful
intercept response.

Wire format is msgpack with positional fields: roughly half the size of JSON
and about 4x faster to pack. Bump WIRE_VERSION if the field order changes;
the worker skips records it cannot decode rather than crashing on them.
"""

from __future__ import annotations

import asyncio
import logging
from typing import List, Optional, Sequence

import msgpack
import redis.asyncio as aioredis

from ..config import Config

log = logging.getLogger(__name__)

WIRE_VERSION = 1

# Move up to N entries from the queue to an in-flight list, atomically.
# RPUSH is chunked because Lua's unpack() blows the C stack past ~7900 args.
_CLAIM_LUA = """
local n = tonumber(ARGV[1])
local items = redis.call('LRANGE', KEYS[1], 0, n - 1)
local count = #items
if count == 0 then return items end
redis.call('LTRIM', KEYS[1], count, -1)
local i = 1
while i <= count do
  local stop = math.min(i + 999, count)
  redis.call('RPUSH', KEYS[2], unpack(items, i, stop))
  i = stop + 1
end
return items
"""

# Push a claimed batch back to the *head* of the queue, preserving order.
#
# LPUSH prepends its arguments left to right, so `LPUSH k a b c` leaves
# [c, b, a]. To get the original order back at the head, each chunk is fed in
# reverse and the chunks are walked from the end of the batch to the start.
# Order matters: records arrive time-sorted, and keeping them that way is what
# lets the DoubleDelta timestamp codec and the minmax skip index do their jobs.
_REQUEUE_LUA = """
local items = redis.call('LRANGE', KEYS[1], 0, -1)
local count = #items
if count == 0 then
  redis.call('DEL', KEYS[1])
  return 0
end
local i = count
while i >= 1 do
  local start = math.max(i - 999, 1)
  local chunk = {}
  local n = 0
  for j = i, start, -1 do
    n = n + 1
    chunk[n] = items[j]
  end
  redis.call('LPUSH', KEYS[2], unpack(chunk, 1, n))
  i = start - 1
end
redis.call('DEL', KEYS[1])
return count
"""


def build_client(cfg: Config) -> aioredis.Redis:
    if cfg.redis.socket_path:
        return aioredis.Redis(
            unix_socket_path=cfg.redis.socket_path,
            db=cfg.redis.db,
            password=cfg.redis.password or None,
            decode_responses=False,
        )
    return aioredis.Redis(
        host=cfg.redis.host,
        port=cfg.redis.port,
        db=cfg.redis.db,
        password=cfg.redis.password or None,
        decode_responses=False,
        socket_keepalive=True,
        health_check_interval=30,
    )


class LogQueue:
    """Producer/consumer wrapper around a Redis list."""

    def __init__(self, client: aioredis.Redis, cfg: Config):
        self.r = client
        self.cfg = cfg
        self.key = cfg.redis.queue_key
        self.max_len = cfg.redis.max_queue_length
        self._claim = client.register_script(_CLAIM_LUA)
        self._requeue = client.register_script(_REQUEUE_LUA)
        self._packer = msgpack.Packer(use_bin_type=True)

    # -- producer ----------------------------------------------------------
    @staticmethod
    def encode(row: Sequence) -> bytes:
        return msgpack.packb(list(row), use_bin_type=True)

    async def push_many(self, payloads: List[bytes]) -> int:
        """Append encoded rows. Returns how many were accepted."""
        if not payloads:
            return 0
        await self.r.rpush(self.key, *payloads)
        return len(payloads)

    async def length(self) -> int:
        try:
            return int(await self.r.llen(self.key))
        except Exception:
            return -1

    async def over_limit(self) -> bool:
        return await self.length() > self.max_len

    # -- consumer ----------------------------------------------------------
    async def claim(self, inflight_key: str, count: int) -> List[bytes]:
        return await self._claim(keys=[self.key, inflight_key], args=[count])

    async def ack(self, inflight_key: str) -> None:
        await self.r.delete(inflight_key)

    async def release(self, inflight_key: str) -> int:
        """Return an unprocessed claim to the head of the queue."""
        return int(await self._requeue(keys=[inflight_key, self.key]))

    async def recover_orphans(self, prefix: str) -> int:
        """Requeue in-flight lists left behind by a crashed or killed worker."""
        recovered = 0
        async for key in self.r.scan_iter(match=f"{prefix}*", count=100):
            name = key.decode() if isinstance(key, bytes) else key
            n = await self.release(name)
            if n:
                log.warning("recovered %d orphaned records from %s", n, name)
                recovered += n
        return recovered

    @staticmethod
    def decode(payload: bytes) -> Optional[list]:
        try:
            row = msgpack.unpackb(payload, raw=False)
        except Exception:
            return None
        return row if isinstance(row, list) and len(row) == 11 else None


async def wait_for_redis(client: aioredis.Redis, attempts: int = 30, delay: float = 1.0) -> bool:
    for i in range(attempts):
        try:
            await client.ping()
            return True
        except Exception as exc:
            log.warning("redis not ready (%s/%s): %s", i + 1, attempts, exc)
            await asyncio.sleep(delay)
    return False
