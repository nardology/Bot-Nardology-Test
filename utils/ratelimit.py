# utils/ratelimit.py
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass


@dataclass
class LimitResult:
    allowed: bool
    retry_after_seconds: int = 0
    used: int = 0
    remaining: int = 0


class SlidingWindowLimiter:
    """
    Sliding window rate limiter using timestamps.
    Good for small bots; upgrade later to Redis if needed.
    """
    def __init__(self, max_events: int, window_seconds: int):
        self.max_events = int(max_events)
        self.window_seconds = int(window_seconds)
        self.events: dict[str, deque[float]] = {}

    def check(self, key: str) -> LimitResult:
        now = time.time()
        q = self.events.setdefault(key, deque())

        # Drop old events
        cutoff = now - self.window_seconds
        while q and q[0] < cutoff:
            q.popleft()

        used_before = len(q)

        # Allow
        if used_before < self.max_events:
            q.append(now)
            used_after = len(q)
            remaining = max(0, self.max_events - used_after)
            return LimitResult(True, 0, used_after, remaining)

        # Deny
        retry_after = int((q[0] + self.window_seconds) - now) + 1
        return LimitResult(False, max(1, retry_after), used_before, 0)
