"""Client-side pacing with continuous-refill token buckets.

What the API counts (from https://platform.claude.com/docs/en/api/rate-limits,
fetched 3 Nov 2026):

* "The API uses the token bucket algorithm ... your capacity is continuously
  replenished up to your maximum limit, rather than being reset at fixed
  intervals." -> one bucket per limit, refilled at limit/60 per second.
* ITPM: "only `input_tokens` + `cache_creation_input_tokens` count toward your
  ITPM limit"; `cache_read_input_tokens` do NOT count (except Claude Haiku 3.5).
  ITPM is "estimated at the beginning of each request, and the estimate is
  adjusted during the request". -> we take an input estimate up front and
  reconcile with input_tokens + cache_creation_input_tokens afterwards.
* OTPM: "evaluated in real time as output tokens are produced, counting only the
  actual tokens generated. The `max_tokens` parameter does not factor into OTPM".
  -> the server does not reserve max_tokens. We still take an output
  *reservation* before sending (the caller chooses it; LLMClient defaults to
  max_tokens, the safe choice) and reconcile with the actual output_tokens,
  which credits the unused part back.
* Headers: anthropic-ratelimit-{requests,input-tokens,output-tokens}-
  {limit,remaining,reset}; token "remaining" values are rounded to the nearest
  thousand; reset is RFC 3339. A 429 may carry ``retry-after`` (seconds).

Waiting never busy-loops: the waiter computes exactly how long until every
bucket can serve it and sleeps that long. Waiters are served in FIFO order
(an asyncio.Lock is fair), so a large request is not starved by small ones.
Clock and sleep are injectable for tests.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping


class TokenBucket:
    """A bucket of ``capacity`` units refilled continuously at ``capacity / period_s``."""

    def __init__(self, capacity: float, period_s: float, clock: Callable[[], float]) -> None:
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        self.capacity = float(capacity)
        self.period_s = float(period_s)
        self._clock = clock
        self.level = float(capacity)
        self._t = clock()

    @property
    def rate(self) -> float:
        """Units per second."""
        return self.capacity / self.period_s

    def refill(self) -> None:
        now = self._clock()
        if now > self._t:
            self.level = min(self.capacity, self.level + (now - self._t) * self.rate)
        self._t = max(self._t, now)

    def wait_time(self, amount: float) -> float:
        """Seconds until ``amount`` units are available (0 if available now)."""
        self.refill()
        amount = min(amount, self.capacity)  # a request larger than the bucket waits for a full bucket
        missing = amount - self.level
        return 0.0 if missing <= 0 else missing / self.rate

    def take(self, amount: float) -> None:
        self.refill()
        self.level -= amount

    def give(self, amount: float) -> None:
        """Credit back (positive) or debit (negative); the level may go below zero."""
        self.refill()
        self.level = min(self.capacity, self.level + amount)

    def set_capacity(self, capacity: float) -> None:
        self.refill()
        self.capacity = float(capacity)
        self.level = min(self.level, self.capacity)


@dataclass
class Permit:
    """What one call took from the buckets; handed back to ``reconcile``."""

    input_tokens: int
    output_tokens: int
    reconciled: bool = False


RATELIMIT_KINDS = {"requests": "requests", "input-tokens": "input", "output-tokens": "output"}


class RateLimiter:
    """Three token buckets (requests, input tokens, output tokens) per minute."""

    def __init__(
        self,
        requests_per_min: float = 1_000,
        input_tokens_per_min: float = 500_000,
        output_tokens_per_min: float = 80_000,
        *,
        safety: float = 0.9,
        low_water: float = 0.2,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    ) -> None:
        """``safety`` scales the configured limits down (default: aim at 90 %).

        ``low_water``: when a response header says less than this fraction of a
        limit remains, the local bucket is lowered to the server's figure.
        """
        self._clock = clock
        self._sleep = sleep
        self.safety = safety
        self.low_water = low_water
        self.buckets: dict[str, TokenBucket] = {
            "requests": TokenBucket(requests_per_min * safety, 60.0, clock),
            "input": TokenBucket(input_tokens_per_min * safety, 60.0, clock),
            "output": TokenBucket(output_tokens_per_min * safety, 60.0, clock),
        }
        self._pause_until = float("-inf")
        self._lock: asyncio.Lock | None = None
        self.total_wait_s = 0.0

    def _get_lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    # ------------------------------------------------------------ acquire
    def _wait_needed(self, amounts: Mapping[str, float]) -> float:
        now = self._clock()
        wait = max(0.0, self._pause_until - now)
        for name, amount in amounts.items():
            wait = max(wait, self.buckets[name].wait_time(amount))
        return wait

    async def acquire(self, input_tokens_est: int, output_tokens_reserved: int) -> Permit:
        """Wait until all three buckets can serve this call, then take from them."""
        amounts = {"requests": 1, "input": input_tokens_est, "output": output_tokens_reserved}
        async with self._get_lock():
            while True:
                wait = self._wait_needed(amounts)
                if wait <= 0:
                    break
                self.total_wait_s += wait
                await self._sleep(wait)
            for name, amount in amounts.items():
                self.buckets[name].take(amount)
        return Permit(int(input_tokens_est), int(output_tokens_reserved))

    def reconcile(
        self,
        permit: Permit,
        actual_input_tokens: int | None = None,
        actual_output_tokens: int | None = None,
    ) -> None:
        """Correct the buckets once real usage is known.

        Pass the ITPM-relevant input (input_tokens + cache_creation_input_tokens)
        and output_tokens. ``None`` means "nothing was consumed" (failed call):
        the whole token reservation is credited back. The request slot is kept.
        """
        if permit.reconciled:
            return
        permit.reconciled = True
        ai = 0 if actual_input_tokens is None else actual_input_tokens
        ao = 0 if actual_output_tokens is None else actual_output_tokens
        self.buckets["input"].give(permit.input_tokens - ai)
        self.buckets["output"].give(permit.output_tokens - ao)

    # ------------------------------------------------------------ server feedback
    def pause_until(self, t: float) -> None:
        """Global pause: no acquire succeeds before clock time ``t``."""
        self._pause_until = max(self._pause_until, t)

    def pause_for(self, seconds: float) -> None:
        self.pause_until(self._clock() + max(0.0, seconds))

    @property
    def paused_until(self) -> float:
        return self._pause_until

    def observe_headers(self, headers: Mapping[str, str] | Any) -> dict[str, Any]:
        """Read anthropic-ratelimit-* headers and slow down when the server is low.

        * If the server reports a smaller limit than configured, the bucket
          capacity is lowered to it (times ``safety``).
        * If remaining / limit < ``low_water``, the local level is lowered to
          the server's remaining figure, so pacing slows down.
        Returns the parsed snapshot.
        """
        snap = ratelimit_snapshot(headers)
        for kind, name in RATELIMIT_KINDS.items():
            limit = _to_float(snap.get(f"anthropic-ratelimit-{kind}-limit"))
            remaining = _to_float(snap.get(f"anthropic-ratelimit-{kind}-remaining"))
            bucket = self.buckets[name]
            if limit and limit * self.safety < bucket.capacity:
                bucket.set_capacity(limit * self.safety)
            if limit and remaining is not None and remaining < self.low_water * limit:
                bucket.refill()
                bucket.level = min(bucket.level, remaining)
        return snap

    def snapshot(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for name, b in self.buckets.items():
            b.refill()
            out[name] = {"level": round(b.level, 1), "capacity": b.capacity}
        out["paused_for_s"] = max(0.0, self._pause_until - self._clock())
        return out


def ratelimit_snapshot(headers: Mapping[str, str] | Any) -> dict[str, str]:
    """Pick the rate-limit related headers (anthropic-ratelimit-*, retry-after)."""
    if headers is None:
        return {}
    items = headers.items() if hasattr(headers, "items") else []
    out: dict[str, str] = {}
    for k, v in items:
        lk = str(k).lower()
        if lk.startswith("anthropic-ratelimit-") or lk == "retry-after":
            out[lk] = str(v)
    return out


def parse_retry_after(value: str | None) -> float | None:
    """Seconds from a ``retry-after`` header value (number of seconds), or None."""
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return None


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
