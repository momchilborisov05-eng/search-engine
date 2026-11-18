"""Thread-safe LRU cache with optional TTL and hit/miss accounting (OrderedDict: O(1) get, put and evict)."""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Hashable
from typing import Any

_MISSING = object()


class LRUCache:
    def __init__(self, capacity: int = 256, ttl: float = 0.0, clock: Callable[[], float] = time.monotonic):
        if capacity < 1:
            raise ValueError("capacity must be >= 1")
        self.capacity = capacity
        self.ttl = ttl
        self._clock = clock
        self._data: OrderedDict[Hashable, tuple[float, Any]] = OrderedDict()
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0
        self.evictions = 0

    def get(self, key: Hashable, default: Any = None) -> Any:
        with self._lock:
            item = self._data.get(key, _MISSING)
            if item is not _MISSING:
                stored_at, value = item
                if self.ttl and self._clock() - stored_at > self.ttl:
                    del self._data[key]
                else:
                    self._data.move_to_end(key)  # most recently used goes to the end
                    self.hits += 1
                    return value
            self.misses += 1
            return default

    def put(self, key: Hashable, value: Any) -> None:
        with self._lock:
            if key in self._data:
                self._data.move_to_end(key)
            self._data[key] = (self._clock(), value)
            while len(self._data) > self.capacity:
                self._data.popitem(last=False)  # least recently used is at the front
                self.evictions += 1

    def clear(self) -> None:
        with self._lock:
            self._data.clear()

    def __contains__(self, key: Hashable) -> bool:
        with self._lock:
            return key in self._data

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)

    @property
    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return self.hits / total if total else 0.0

    def stats(self) -> dict:
        with self._lock:
            size = len(self._data)
        return {
            "capacity": self.capacity,
            "size": size,
            "hits": self.hits,
            "misses": self.misses,
            "evictions": self.evictions,
            "hit_rate": round(self.hit_rate, 4),
            "ttl_seconds": self.ttl,
        }
