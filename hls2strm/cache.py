"""容量受限的异步缓存；并发读取合并，失败不缓存，失效时不发布旧结果。"""

from __future__ import annotations

import asyncio
import copy
import time
from collections import OrderedDict


class AsyncCache:
    def __init__(self, capacity: int = 256):
        self.capacity = capacity
        self._values = OrderedDict()
        self._pending = {}
        self._waiters = {}
        self._generation = 0

    def clear(self):
        self._generation += 1
        self._values.clear()

    async def close(self):
        tasks = list(self._pending.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.clear()

    async def get(self, key, load, ttl: float):
        entry = self._values.get(key)
        if entry and entry[0] > time.monotonic():
            self._values.move_to_end(key)
            return copy.deepcopy(entry[1])
        generation = self._generation
        flight = (generation, key)
        task = self._pending.get(flight)
        if task is None:
            async def fill():
                try:
                    value = await load()
                    if generation == self._generation:
                        self._values[key] = (time.monotonic() + ttl, value)
                        self._values.move_to_end(key)
                        while len(self._values) > self.capacity:
                            self._values.popitem(last=False)
                    return value
                finally:
                    self._pending.pop(flight, None)
            task = asyncio.create_task(fill())
            self._pending[flight] = task
        self._waiters[flight] = self._waiters.get(flight, 0) + 1
        try:
            return copy.deepcopy(await asyncio.shield(task))
        finally:
            self._waiters[flight] -= 1
            if not self._waiters[flight]:
                self._waiters.pop(flight)
                if not task.done():
                    task.cancel()
