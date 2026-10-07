"""Resilience building blocks: retry policy and circuit breaker.

RETRY — only where it is safe and useful:
  * only idempotent reads (agents only ever GET),
  * only FAST, TRANSIENT failures: connection refused, 502, 503 (e.g. a container restarting),
  * never timeouts (a retry would blow the time budget) or 4xx / 429 (retrying won't help),
  * only at the layer closest to the failure, so retries don't multiply across layers
    (3 layers x 3 attempts would turn 1 request into 27).

CIRCUIT BREAKER — stop calling a backend that keeps failing:

    CLOSED ──(N consecutive failures)──> OPEN ──(after reset_timeout)──> HALF_OPEN
      ^                                   │ calls fail fast,                │ one trial call
      └──────────(trial succeeds)─────────┴── no request is sent ◄──(trial fails)

  While OPEN, callers get an immediate CIRCUIT_OPEN error instead of waiting for timeouts,
  and the struggling backend gets breathing room to recover.
"""
from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass
from typing import Callable

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 2          # 1 call + 1 retry
    base_delay: float = 0.3        # seconds; doubles each retry
    max_delay: float = 2.0

    def delay(self, attempt: int) -> float:
        """Exponential backoff with jitter, so many clients don't retry in lockstep."""
        d = min(self.max_delay, self.base_delay * (2 ** (attempt - 1)))
        return d * random.uniform(0.8, 1.2)


class CircuitBreaker:
    CLOSED, OPEN, HALF_OPEN = "closed", "open", "half_open"

    def __init__(self, name: str, failure_threshold: int = 3, reset_timeout: float = 15.0,
                 clock: Callable[[], float] = time.monotonic):
        self.name = name
        self.failure_threshold = failure_threshold
        self.reset_timeout = reset_timeout
        self._clock = clock
        self.state = self.CLOSED
        self.failures = 0
        self.opened_at = 0.0
        self._trial_in_flight = False

    def allow(self) -> bool:
        """May a call go through right now?"""
        if self.state == self.OPEN:
            if self._clock() - self.opened_at < self.reset_timeout:
                return False
            self._set(self.HALF_OPEN)
        if self.state == self.HALF_OPEN:
            if self._trial_in_flight:
                return False           # exactly one trial call while half-open
            self._trial_in_flight = True
        return True

    def record_success(self) -> None:
        self._trial_in_flight = False
        self.failures = 0
        if self.state != self.CLOSED:
            self._set(self.CLOSED)

    def record_failure(self) -> None:
        self._trial_in_flight = False
        self.failures += 1
        if self.state == self.HALF_OPEN or self.failures >= self.failure_threshold:
            self.opened_at = self._clock()
            if self.state != self.OPEN:
                self._set(self.OPEN)

    def release(self) -> None:
        """A call finished without telling us anything about backend health (e.g. 404)."""
        self._trial_in_flight = False

    def retry_in(self) -> float:
        return max(0.0, self.reset_timeout - (self._clock() - self.opened_at)) if self.state == self.OPEN else 0.0

    def _set(self, state: str) -> None:
        logger.warning("circuit state changed", extra={"fields": {
            "event": "circuit.state", "circuit": self.name, "from": self.state, "to": state,
            "failures": self.failures}})
        self.state = state

    def snapshot(self) -> dict:
        return {"state": self.state, "consecutive_failures": self.failures,
                "retry_in_seconds": round(self.retry_in(), 1)}
