"""Sliding-window rate limiter kept in process memory.

Correct for a single API instance (the free-tier deployment). Running several instances needs a shared store
(Postgres or Redis) behind the same RateLimiter port.
"""

import asyncio
import math
import time
from collections import deque
from collections.abc import Callable

from app.domain.errors import RateLimitedError


class InMemoryRateLimiter:
    def __init__(self, clock: Callable[[], float] = time.monotonic, max_keys: int = 50_000) -> None:
        self._clock = clock
        self._max_keys = max_keys
        self._hits: dict[str, deque[float]] = {}
        self._lock = asyncio.Lock()

    async def hit(self, key: str, *, limit: int, window_s: int) -> None:
        async with self._lock:
            now = self._clock()
            hits = self._hits.setdefault(key, deque())
            while hits and hits[0] <= now - window_s:
                hits.popleft()
            if len(hits) >= limit:
                raise RateLimitedError(key.split(":", 1)[0], max(1, math.ceil(hits[0] + window_s - now)))
            hits.append(now)
            if len(self._hits) > self._max_keys:
                self._evict_idle(now, window_s)

    def _evict_idle(self, now: float, window_s: int) -> None:
        for k in [k for k, v in self._hits.items() if not v or v[-1] <= now - window_s]:
            del self._hits[k]
