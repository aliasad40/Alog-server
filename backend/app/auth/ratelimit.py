"""Login throttling backed by Redis.

Counted per (username, source IP) so one attacker cannot lock out a real
administrator by hammering their username from elsewhere, while still
stopping password spraying from a single host.
"""

from __future__ import annotations

import logging
from typing import Tuple

log = logging.getLogger(__name__)


class LoginThrottle:
    def __init__(self, redis_client, max_failures: int, lockout_seconds: int):
        self.r = redis_client
        self.max_failures = max_failures
        self.lockout = lockout_seconds

    @staticmethod
    def _key(username: str, ip: str) -> str:
        return f"nls:login_fail:{ip}:{username[:64]}"

    async def check(self, username: str, ip: str) -> Tuple[bool, int]:
        """Returns (allowed, seconds_remaining)."""
        try:
            key = self._key(username, ip)
            count = await self.r.get(key)
            if count is None:
                return True, 0
            if int(count) < self.max_failures:
                return True, 0
            ttl = await self.r.ttl(key)
            return False, max(int(ttl), 0)
        except Exception as exc:
            # Never lock everyone out because Redis hiccuped.
            log.warning("login throttle unavailable: %s", exc)
            return True, 0

    async def record_failure(self, username: str, ip: str) -> None:
        try:
            key = self._key(username, ip)
            pipe = self.r.pipeline(transaction=True)
            pipe.incr(key)
            pipe.expire(key, self.lockout)
            await pipe.execute()
        except Exception:
            pass

    async def clear(self, username: str, ip: str) -> None:
        try:
            await self.r.delete(self._key(username, ip))
        except Exception:
            pass
