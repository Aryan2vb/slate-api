"""In-process sign-in hand-off store: short-lived, one-time, bounded.

Correct for a single API instance. Losing it on restart only means the user signs in again.
"""

import asyncio
import time
from collections import OrderedDict
from collections.abc import Callable


class InMemoryAuthHandoffs:
    def __init__(self, ttl_s: float = 300, max_items: int = 10_000, clock: Callable[[], float] = time.monotonic):
        self._ttl_s = ttl_s
        self._max_items = max_items
        self._clock = clock
        self._items: OrderedDict[str, tuple[float, dict[str, str]]] = OrderedDict()
        self._lock = asyncio.Lock()

    async def put(self, handoff_id: str, result: dict[str, str]) -> None:
        async with self._lock:
            self._evict()
            self._items[handoff_id] = (self._clock() + self._ttl_s, dict(result))
            self._items.move_to_end(handoff_id)
            while len(self._items) > self._max_items:
                self._items.popitem(last=False)

    async def take(self, handoff_id: str) -> dict[str, str] | None:
        async with self._lock:
            self._evict()
            item = self._items.pop(handoff_id, None)
            return item[1] if item else None

    def _evict(self) -> None:
        now = self._clock()
        for key in [k for k, (expires, _) in self._items.items() if expires <= now]:
            del self._items[key]
