"""Sliding-window rate limiter shared across the bot.

Used both for inbound OTP pushes from the API (``internal_server``) and for
user-initiated OTP requests via the contact-share flow (``handlers``). Cheap
and in-memory; sufficient for a single-instance bot. Switch to Redis if the
bot ever runs multiple replicas.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque


class RateLimiter:
    """Per-key sliding-window rate limiter.

    Each key gets a deque of recent timestamps; entries outside the window are
    discarded on every check. ``allow`` is the only mutator; ``retry_after``
    is a read-only helper for friendly user messaging.
    """

    def __init__(self, max_per_window: int, window_seconds: float = 60.0) -> None:
        self._max = max_per_window
        self._window = window_seconds
        self._buckets: dict[int, deque[float]] = defaultdict(deque)

    def allow(self, key: int) -> bool:
        """Record an attempt for ``key``; return True if it's under the limit."""
        now = time.monotonic()
        bucket = self._buckets[key]
        cutoff = now - self._window
        while bucket and bucket[0] <= cutoff:
            bucket.popleft()
        if len(bucket) >= self._max:
            return False
        bucket.append(now)
        return True

    def retry_after(self, key: int) -> float:
        """Seconds until the oldest entry expires (the next slot frees up).

        Returns 0 if the key is not currently at the limit. Does not mutate.
        """
        bucket = self._buckets.get(key)
        if not bucket or len(bucket) < self._max:
            return 0.0
        return max(0.0, bucket[0] + self._window - time.monotonic())
