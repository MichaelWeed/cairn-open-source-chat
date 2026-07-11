"""In-memory token-bucket rate limiting.

Per-key buckets are unbounded in number (one per distinct IP/session seen).
Acceptable for the single-instance design point (DEVELOPER_README.md §9);
revisit if bucket cardinality becomes a concern under sustained abuse.
"""

import time


class TokenBucket:
    def __init__(self, capacity: float, refill_per_second: float) -> None:
        self._capacity = capacity
        self._refill_per_second = refill_per_second
        self._tokens = capacity
        self._last_refill = time.monotonic()

    def consume(self, amount: float = 1.0) -> bool:
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._last_refill = now
        self._tokens = min(self._capacity, self._tokens + elapsed * self._refill_per_second)
        if self._tokens >= amount:
            self._tokens -= amount
            return True
        return False


class RateLimiter:
    def __init__(self, capacity: float, refill_per_minute: float) -> None:
        self._capacity = capacity
        self._refill_per_second = refill_per_minute / 60.0
        self._buckets: dict[str, TokenBucket] = {}

    def allow(self, key: str) -> bool:
        bucket = self._buckets.get(key)
        if bucket is None:
            bucket = TokenBucket(self._capacity, self._refill_per_second)
            self._buckets[key] = bucket
        return bucket.consume()
