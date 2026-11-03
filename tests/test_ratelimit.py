import asyncio

import pytest

from harness.ratelimit import RateLimiter, TokenBucket, parse_retry_after


def test_bucket_refills_continuously(fake_clock):
    b = TokenBucket(60, 60.0, fake_clock)  # 1 unit per second
    b.take(60)
    assert b.wait_time(1) == pytest.approx(1.0)
    fake_clock.t += 0.5
    assert b.wait_time(1) == pytest.approx(0.5)
    fake_clock.t += 100
    b.refill()
    assert b.level == 60  # capped at capacity
    # a request larger than the bucket waits for a full bucket, not forever
    b.take(60)
    assert b.wait_time(1_000) == pytest.approx(60.0)


def make(fake_clock, **kw):
    return RateLimiter(
        kw.pop("rpm", 60), kw.pop("itpm", 6_000), kw.pop("otpm", 600),
        safety=1.0, clock=fake_clock, sleep=fake_clock.sleep, **kw,
    )


async def test_acquire_waits_exact_time_without_busy_loop(fake_clock):
    lim = make(fake_clock)
    await lim.acquire(6_000, 0)  # drains the input bucket (100 tokens/s)
    t0 = fake_clock.t
    await lim.acquire(500, 0)  # needs 500 tokens -> 5 s
    assert fake_clock.t - t0 == pytest.approx(5.0)
    assert len(fake_clock.sleeps) == 1  # one computed sleep, no polling


async def test_requests_bucket(fake_clock):
    lim = make(fake_clock, rpm=2)  # one request every 30 s after the burst
    await lim.acquire(1, 1)
    await lim.acquire(1, 1)
    t0 = fake_clock.t
    await lim.acquire(1, 1)
    assert fake_clock.t - t0 == pytest.approx(30.0)


async def test_reconcile_credits_and_debits(fake_clock):
    lim = make(fake_clock)
    p = await lim.acquire(1_000, 600)
    assert lim.buckets["output"].level == pytest.approx(0)
    lim.reconcile(p, actual_input_tokens=400, actual_output_tokens=100)
    assert lim.buckets["output"].level == pytest.approx(500)
    assert lim.buckets["input"].level == pytest.approx(5_600)
    lim.reconcile(p, 0, 0)  # second reconcile is ignored
    assert lim.buckets["output"].level == pytest.approx(500)
    p2 = await lim.acquire(100, 0)
    lim.reconcile(p2, actual_input_tokens=2_000, actual_output_tokens=0)  # underestimate -> debit
    assert lim.buckets["input"].level == pytest.approx(5_600 - 2_000)


async def test_global_pause_blocks_all_workers(fake_clock):
    lim = make(fake_clock)
    lim.pause_for(7.0)
    t0 = fake_clock.t
    await asyncio.gather(lim.acquire(1, 1), lim.acquire(1, 1), lim.acquire(1, 1))
    assert fake_clock.t - t0 == pytest.approx(7.0)


async def test_observe_headers_slows_pacing(fake_clock):
    lim = make(fake_clock, otpm=80_000)
    snap = lim.observe_headers(
        {
            "anthropic-ratelimit-output-tokens-limit": "80000",
            "anthropic-ratelimit-output-tokens-remaining": "1000",
            "anthropic-ratelimit-input-tokens-limit": "500000",
            "anthropic-ratelimit-input-tokens-remaining": "490000",
            "content-type": "application/json",
        }
    )
    assert "content-type" not in snap
    assert lim.buckets["output"].level == pytest.approx(1_000)
    # the server says the input limit is lower than configured (6,000 here is lower, so unchanged)
    assert lim.buckets["input"].capacity == 6_000
    t0 = fake_clock.t
    await lim.acquire(1, 2_000)  # needs 1,000 more output tokens at 80,000/60 per second
    assert fake_clock.t - t0 == pytest.approx(1_000 / (80_000 / 60))


def test_observe_headers_lowers_capacity(fake_clock):
    lim = make(fake_clock, rpm=4_000)
    lim.observe_headers({"anthropic-ratelimit-requests-limit": "1000", "anthropic-ratelimit-requests-remaining": "900"})
    assert lim.buckets["requests"].capacity == 1_000


def test_parse_retry_after():
    assert parse_retry_after("2") == 2.0
    assert parse_retry_after("0.5") == 0.5
    assert parse_retry_after(None) is None
    assert parse_retry_after("soon") is None
