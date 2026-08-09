import threading
import time
from typing import Any

from src.observability.metrics import CACHE_HITS, CACHE_MISSES


class TTLCache:
    def __init__(self) -> None:
        self._values: dict[str, tuple[float, Any]] = {}
        self._lock = threading.RLock()

    def get(self, key: str) -> Any | None:
        with self._lock:
            expires, value = self._values.get(key, (0, None))
            if expires <= time.monotonic():
                self._values.pop(key, None)
                CACHE_MISSES.labels(cache=key.split(":", 1)[0]).inc()
                return None
            CACHE_HITS.labels(cache=key.split(":", 1)[0]).inc()
            return value

    def set(self, key: str, value: Any, ttl_seconds: int) -> None:
        with self._lock:
            self._values[key] = (time.monotonic() + ttl_seconds, value)

    def delete_prefix(self, prefix: str) -> None:
        with self._lock:
            for key in [key for key in self._values if key.startswith(prefix)]:
                self._values.pop(key, None)


cache = TTLCache()
user_locks: dict[str, threading.Lock] = {}
locks_guard = threading.Lock()


def generation_lock(user_id: str) -> threading.Lock:
    with locks_guard:
        return user_locks.setdefault(user_id, threading.Lock())

