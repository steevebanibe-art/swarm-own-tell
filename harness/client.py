"""LLMClient: one safe, budgeted, rate-limited, retried Messages API call.

For every attempt the client:
1. waits for a concurrency slot (asyncio.Semaphore);
2. reserves the call's worst-case cost in the CostLedger (raises
   BudgetExceeded *before* anything is sent);
3. waits for the RateLimiter (requests, input tokens, output tokens);
4. sends the request with ``messages.with_raw_response.create`` to read the
   headers, then reconciles the rate limiter and settles the real cost.

Retries (tenacity): 429, 5xx (including 529 overloaded), connection errors and
timeouts. Never other 4xx. Error classes checked in the installed SDK
(anthropic 2.4.0, transport httpx2): RateLimitError (429), OverloadedError
(529), InternalServerError (other 5xx), all subclasses of APIStatusError;
APITimeoutError is a subclass of APIConnectionError. A 429 with ``retry-after`` pauses *all* workers for
that long (global pause in the RateLimiter) and the retry waits at least that
long; otherwise exponential backoff with jitter. A 429 whose body says
``enforced_spend_limit_reached`` (monthly spend cap) is not retried.

Input-token estimate: ``len(json.dumps(request)) / CHARS_PER_TOKEN`` with
``CHARS_PER_TOKEN = 2.5``. English prose is about 4 characters per token and
the JSON wrapper adds characters that are not tokens, so 2.5 over-estimates
input tokens for typical prompts (safe for both the budget and ITPM); the
estimate is reconciled with the real usage after the call.

Money safety: a request that may have reached the API is never released for
free. After a timeout, a connection error, or a cancellation / Ctrl-C that
arrives once the request may be on the wire, the call's worst case is written
to the ledger (``estimated: true``), so the hard cap stays a true upper bound.
Exception: when no connection could be opened at all (httpx2 ConnectError,
ConnectTimeout or PoolTimeout under the SDK error), nothing was sent, so the
reservation is simply released. Otherwise a long network outage would fill the
ledger with phantom worst-case charges and stop the run on the cap.

Optional response cache (``cache=``, a JsonlStore or a path): every successful
call is stored by its key together with a hash of the full request. A later
call with the same key and the same request returns the stored result
(``cached=True``) without reserving or sending anything, so a task made of
many calls that is interrupted and re-run replays its finished calls for free
and with the same answers. Same key with a different request: a warning, and
the call is sent again. Keys must therefore name one specific call.

The client knows nothing about any prompt: system, messages, max_tokens,
thinking, output_config and any other parameter are passed through unchanged.
``temperature``, ``top_p`` and ``top_k`` are rejected with ValueError (the API
returns 400 for them on Claude Haiku 5.5 and on API version 2026-10-21).
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import os
import random
import time
import warnings
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

import anthropic
import httpx2  # the SDK's transport since anthropic 1.x (installed: 2.4.0 on httpx2 2.16.0)
from tenacity import AsyncRetrying, RetryCallState, retry_if_exception, stop_after_attempt

from harness.budget import BudgetExceeded, CostLedger, Reservation
from harness.jsonl_store import JsonlStore, canonical_json
from harness.pricing import (
    CACHE_WRITE_1H_MULTIPLIER,
    CACHE_WRITE_5M_MULTIPLIER,
    cost_usd,
    usage_to_dict,
)
from harness.ratelimit import Permit, RateLimiter, parse_retry_after, ratelimit_snapshot

CHARS_PER_TOKEN = 2.5
TOKEN_OVERHEAD = 16  # fixed per-request allowance for role/format tokens
FORBIDDEN_PARAMS = ("temperature", "top_p", "top_k")


@dataclass
class CallResult:
    """Outcome of one successful call (after retries)."""

    key: str
    model: str
    text: str
    stop_reason: str | None
    refusal: bool
    refusal_category: str | None
    usage: dict[str, Any]
    cost_usd: float
    latency_s: float
    attempts: int
    request_id: str | None
    ratelimit: dict[str, str] = field(default_factory=dict)
    refusal_explanation: str | None = None
    cached: bool = False  # True when returned from the response cache (nothing sent or paid)

    def to_record(self) -> dict[str, Any]:
        """Plain dict, ready for a JsonlStore."""
        return dict(self.__dict__)


def check_params(params: dict[str, Any]) -> None:
    """Reject parameters the target models refuse with a 400."""
    bad = [p for p in FORBIDDEN_PARAMS if p in params]
    if bad:
        raise ValueError(
            f"Parameter(s) {', '.join(bad)} must not be sent (400 on Claude Haiku 5.5 and on "
            "API version 2026-10-21). Remove them."
        )


NOT_PROMPT_FIELDS = ("model", "max_tokens", "stream", "metadata")


def estimate_input_tokens(request: dict[str, Any]) -> int:
    """Conservative input-token estimate from the serialized request.

    Every field that can become prompt tokens is counted (system, messages,
    tools, tool_choice, output_config with any JSON schema, thinking, ...);
    only model, max_tokens, stream and metadata are left out.
    """
    serial = json.dumps(
        {k: v for k, v in request.items() if k not in NOT_PROMPT_FIELDS},
        ensure_ascii=False,
        default=str,
    )
    return int(len(serial) / CHARS_PER_TOKEN) + TOKEN_OVERHEAD


def cache_write_multiplier(request: dict[str, Any]) -> float:
    """Worst-case input multiplier: 2.0 for a 1-hour cache write, 1.25 for any cache_control."""
    serial = json.dumps(request, default=str)
    if '"cache_control"' not in serial:
        return 1.0
    if '"ttl": "1h"' in serial:
        return CACHE_WRITE_1H_MULTIPLIER
    return CACHE_WRITE_5M_MULTIPLIER


def request_hash(request: dict[str, Any]) -> str:
    """sha256 of the canonical JSON of the full request (model, params, prompt)."""
    return hashlib.sha256(canonical_json(request).encode("utf-8")).hexdigest()


_RESULT_FIELDS = {f.name for f in dataclasses.fields(CallResult)}


def itpm_input(usage: dict[str, Any]) -> int:
    """Input tokens that count toward ITPM: uncached input + cache writes (not cache reads)."""
    return int(usage.get("input_tokens") or 0) + int(usage.get("cache_creation_input_tokens") or 0)


def is_retryable(exc: BaseException) -> bool:
    """429, 5xx (incl. 529), connection errors and timeouts. Never other 4xx."""
    if isinstance(exc, BudgetExceeded):
        return False
    if isinstance(exc, (anthropic.APITimeoutError, anthropic.APIConnectionError)):
        return True
    if isinstance(exc, anthropic.APIStatusError):
        if exc.status_code == 429:
            return not _is_spend_cap(exc)
        return exc.status_code >= 500
    return False


# Transport errors raised before any byte of the request left this machine:
# no connection could be opened (DNS failure, refused, connect timeout) or no
# pooled connection became free. The SDK wraps them as APIConnectionError /
# APITimeoutError ``from`` the httpx2 error, so they sit on the __cause__ chain.
_NOT_SENT = (httpx2.ConnectError, httpx2.ConnectTimeout, httpx2.PoolTimeout)


def never_sent(exc: BaseException) -> bool:
    """True when a connection error proves the request was never sent (so never billed)."""
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        if isinstance(cur, _NOT_SENT):
            return True
        seen.add(id(cur))
        cur = cur.__cause__  # explicit ``raise ... from`` links only
    return False


def _is_spend_cap(exc: anthropic.APIStatusError) -> bool:
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        err = body.get("error", body)
        details = err.get("details") if isinstance(err, dict) else None
        if isinstance(details, dict) and details.get("error_code") == "enforced_spend_limit_reached":
            return True
    return False


def _retry_after(exc: BaseException | None) -> float | None:
    response = getattr(exc, "response", None)
    if response is None:
        return None
    try:
        return parse_retry_after(response.headers.get("retry-after"))
    except Exception:
        return None


class LLMClient:
    """Budgeted, rate-limited, retrying wrapper around ``anthropic.AsyncAnthropic``."""

    def __init__(
        self,
        ledger: CostLedger,
        limiter: RateLimiter | None = None,
        client: anthropic.AsyncAnthropic | None = None,
        *,
        max_concurrency: int = 8,
        max_attempts: int = 6,
        backoff_base_s: float = 1.0,
        backoff_max_s: float = 60.0,
        charge_unknown_outcomes: bool = True,
        cache: JsonlStore | str | os.PathLike[str] | None = None,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    ) -> None:
        """``client`` defaults to ``AsyncAnthropic(max_retries=0)`` (key read from the environment).

        ``charge_unknown_outcomes``: after a timeout, a connection error or a
        cancellation while the request may be in flight, the request may still
        have been billed, so its worst case is written to the ledger (marked
        ``estimated``). This keeps the hard cap a true upper bound.
        ``cache``: optional call-level response cache (see the module docstring).
        """
        self.ledger = ledger
        self.limiter = limiter or RateLimiter()
        self.client = client if client is not None else anthropic.AsyncAnthropic(max_retries=0)
        self.max_attempts = max_attempts
        self.backoff_base_s = backoff_base_s
        self.backoff_max_s = backoff_max_s
        self.charge_unknown_outcomes = charge_unknown_outcomes
        self._sleep = sleep
        self._sem = asyncio.Semaphore(max_concurrency)
        if cache is not None and not isinstance(cache, JsonlStore):
            cache = JsonlStore(cache)
        self.cache: JsonlStore | None = cache
        self._cache_index: dict[str, dict[str, Any]] = {}
        if cache is not None:
            for rec in cache.iter_records():
                if rec.get("key") is not None:
                    self._cache_index[str(rec["key"])] = rec  # last record wins

    # ------------------------------------------------------------------ wait
    def _wait(self, state: RetryCallState) -> float:
        exc = state.outcome.exception() if state.outcome is not None else None
        ra = _retry_after(exc)
        n = state.attempt_number
        backoff = min(self.backoff_max_s, self.backoff_base_s * (2 ** (n - 1)))
        backoff = backoff * (0.5 + random.random() * 0.5)  # jitter in [50 %, 100 %]
        if ra is not None:
            return ra + random.random() * 0.25 * min(1.0, ra + 0.1)
        return backoff

    # ------------------------------------------------------------------ call
    async def call(
        self,
        key: str,
        *,
        model: str,
        max_tokens: int,
        messages: list[dict[str, Any]],
        output_reservation: int | None = None,
        **params: Any,
    ) -> CallResult:
        """Send one Messages API request, with retries. Extra kwargs pass through unchanged.

        ``output_reservation``: output tokens taken from the OTPM bucket before
        sending (default ``max_tokens``); reconciled with the real output.
        """
        check_params(params)
        request: dict[str, Any] = {"model": model, "max_tokens": max_tokens, "messages": messages, **params}
        req_hash = request_hash(request) if self.cache is not None else None
        if req_hash is not None:
            hit = self._cached(key, req_hash)
            if hit is not None:
                return hit
        est_in = estimate_input_tokens(request)
        worst = self.ledger.worst_case(model, est_in, max_tokens, input_multiplier=cache_write_multiplier(request))
        out_res = max_tokens if output_reservation is None else int(output_reservation)
        attempts = 0

        async def attempt() -> CallResult:
            nonlocal attempts
            attempts += 1
            async with self._sem:
                reservation = self.ledger.reserve(worst)  # BudgetExceeded: nothing sent
                permit: Permit | None = None
                raw: Any = None
                try:
                    permit = await self.limiter.acquire(est_in, out_res)
                    t0 = time.perf_counter()
                    raw = await self.client.messages.with_raw_response.create(**request)
                    msg = await _maybe_await(raw.parse())
                    latency = time.perf_counter() - t0
                except anthropic.APIStatusError as exc:
                    self.ledger.release(reservation)
                    if permit is not None:
                        self.limiter.reconcile(permit, None, None)
                    self._after_error(exc)
                    raise
                except (anthropic.APITimeoutError, anthropic.APIConnectionError) as exc:
                    if never_sent(exc):
                        self.ledger.release(reservation)  # no connection: nothing can be billed
                    else:
                        self._charge_unknown(reservation, key, model, exc)
                    if permit is not None:
                        self.limiter.reconcile(permit, None, None)
                    raise
                except BaseException as exc:
                    # Cancellation / Ctrl-C after the rate limiter let us through: the
                    # request may already be on the wire and billed. Same if the
                    # response arrived and only reading it failed. Charge the worst
                    # case rather than lose real spend.
                    maybe_sent = permit is not None and (
                        raw is not None or isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt))
                    )
                    if maybe_sent:
                        self._charge_unknown(reservation, key, model, exc)
                    else:
                        self.ledger.release(reservation)
                    if permit is not None:
                        self.limiter.reconcile(permit, None, None)
                    raise
                result = self._settle(key, model, raw, msg, latency, reservation, permit, attempts)
                if req_hash is not None:
                    self._store_cached(result, req_hash)
                return result

        retrying = AsyncRetrying(
            retry=retry_if_exception(is_retryable),
            stop=stop_after_attempt(self.max_attempts),
            wait=self._wait,
            sleep=self._sleep,
            reraise=True,
        )
        return await retrying(attempt)

    # ------------------------------------------------------------------ cache
    def _cached(self, key: str, req_hash: str) -> CallResult | None:
        rec = self._cache_index.get(key)
        if rec is None:
            return None
        if rec.get("request_hash") != req_hash:
            warnings.warn(
                f"response cache: key {key!r} was stored for a different request; sending it again",
                RuntimeWarning,
                stacklevel=3,
            )
            return None
        fields = {k: v for k, v in rec.items() if k in _RESULT_FIELDS}
        fields["cached"] = True
        return CallResult(**fields)

    def _store_cached(self, result: CallResult, req_hash: str) -> None:
        rec = result.to_record()
        rec["request_hash"] = req_hash
        assert self.cache is not None
        self.cache.append(rec)  # fsync'd
        self._cache_index[result.key] = rec

    # ------------------------------------------------------------------ helpers
    def _after_error(self, exc: anthropic.APIStatusError) -> None:
        headers = getattr(getattr(exc, "response", None), "headers", None)
        if headers is not None:
            self.limiter.observe_headers(headers)
        if exc.status_code == 429:
            ra = _retry_after(exc)
            if ra is not None:
                self.limiter.pause_for(ra)  # every worker waits, not only this one

    def _charge_unknown(self, reservation: Reservation, key: str, model: str, exc: BaseException) -> None:
        if not self.charge_unknown_outcomes:
            self.ledger.release(reservation)
            return
        self.ledger.settle(
            reservation,
            key=key,
            model=model,
            usage=None,
            cost_usd=reservation.amount_usd,
            extra={"estimated": True, "error": type(exc).__name__},
        )

    def _settle(
        self,
        key: str,
        model: str,
        raw: Any,
        msg: Any,
        latency: float,
        reservation: Reservation,
        permit: Permit | None,
        attempts: int,
    ) -> CallResult:
        usage = usage_to_dict(getattr(msg, "usage", None))
        billed_model = model  # price by the requested id (the price table is keyed by it)
        cost = cost_usd(billed_model, usage)
        self.ledger.settle(reservation, key=key, model=billed_model, usage=usage, cost_usd=cost)
        if permit is not None:
            self.limiter.reconcile(permit, itpm_input(usage), usage["output_tokens"])
        headers = getattr(raw, "headers", None)
        snap = self.limiter.observe_headers(headers) if headers is not None else {}

        text = "".join(
            getattr(b, "text", "") for b in (getattr(msg, "content", None) or []) if getattr(b, "type", None) == "text"
        )
        stop_reason = getattr(msg, "stop_reason", None)
        details = getattr(msg, "stop_details", None)
        category = getattr(details, "category", None) if details is not None else None
        explanation = getattr(details, "explanation", None) if details is not None else None
        request_id = None
        if headers is not None:
            request_id = headers.get("request-id")
        request_id = request_id or getattr(msg, "_request_id", None)
        return CallResult(
            key=key,
            model=model,
            text=text,
            stop_reason=stop_reason,
            refusal=stop_reason == "refusal",
            refusal_category=category,
            refusal_explanation=explanation,
            usage=usage,
            cost_usd=cost,
            latency_s=latency,
            attempts=attempts,
            request_id=request_id,
            ratelimit=snap,
        )

    async def aclose(self) -> None:
        await self.client.close()


async def _maybe_await(value: Any) -> Any:
    if asyncio.iscoroutine(value) or isinstance(value, asyncio.Future):
        return await value
    return value
