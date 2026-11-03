import json

import anthropic
import httpx2
import pytest

from harness.budget import BudgetExceeded, CostLedger
from harness.client import LLMClient, estimate_input_tokens
from harness.pricing import cost_usd
from harness.ratelimit import RateLimiter
from tests.conftest import RATE_HEADERS, error_body, message_body, read_lines

M = "claude-haiku-5-5"
MSGS = [{"role": "user", "content": "Hello"}]


def build(tmp_path, mock_api, fake_clock, cap=22.0, **kw):
    ledger = CostLedger(tmp_path / "ledger.jsonl", hard_cap_usd=cap)
    limiter = RateLimiter(1_000, 500_000, 80_000, clock=fake_clock, sleep=fake_clock.sleep)
    llm = LLMClient(ledger, limiter, mock_api.client(), sleep=fake_clock.sleep, **kw)
    return llm, ledger, limiter


async def test_success_cost_and_one_ledger_line(tmp_path, mock_api, fake_clock):
    usage = {"input_tokens": 1200, "output_tokens": 300, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
    mock_api.message_responses.append((200, message_body(usage=usage), RATE_HEADERS))
    llm, ledger, limiter = build(tmp_path, mock_api, fake_clock)
    r = await llm.call(
        "k1", model=M, max_tokens=512, messages=MSGS, system="Be brief.",
        output_config={"effort": "low"}, thinking={"type": "disabled"},
    )
    assert r.text == "ok" and r.stop_reason == "end_turn" and not r.refusal
    assert r.cost_usd == pytest.approx(cost_usd(M, usage)) == pytest.approx((1200 * 0.1 + 300 * 0.5) / 1e6)
    assert r.attempts == 1 and r.request_id == "req_ok"
    assert r.ratelimit["anthropic-ratelimit-output-tokens-limit"] == "80000"
    lines = read_lines(tmp_path / "ledger.jsonl")
    assert len(lines) == 1 and lines[0]["key"] == "k1" and lines[0]["batch"] is False
    assert ledger.outstanding_usd == 0
    # params passed through unchanged, no sampling params added
    sent = json.loads(mock_api.message_calls()[0].content)
    assert sent["output_config"] == {"effort": "low"} and sent["thinking"] == {"type": "disabled"}
    assert sent["system"] == "Be brief." and sent["max_tokens"] == 512
    assert not {"temperature", "top_p", "top_k"} & set(sent)
    # only the fake test key ever leaves the client (anthropic 2.4 sends it as a Bearer token)
    h = mock_api.message_calls()[0].headers
    assert h.get("authorization") == "Bearer test-key" and h.get("x-api-key") in (None, "test-key")
    # rate limiter reconciled: output bucket credited back to max_tokens - actual
    assert limiter.buckets["output"].level == pytest.approx(80_000 * 0.9 - 300, abs=1)


async def test_thinking_block_before_text(tmp_path, mock_api, fake_clock):
    content = [
        {"type": "thinking", "thinking": "let me think", "signature": "sig"},
        {"type": "text", "text": "Hello"},
        {"type": "text", "text": " world"},
    ]
    mock_api.message_responses.append((200, message_body(content), RATE_HEADERS))
    llm, *_ = build(tmp_path, mock_api, fake_clock)
    r = await llm.call("k", model=M, max_tokens=64, messages=MSGS)
    assert r.text == "Hello world"


async def test_refusal(tmp_path, mock_api, fake_clock):
    body = message_body(
        [], stop_reason="refusal",
        stop_details={"type": "refusal", "category": "cyber", "explanation": "declined"},
    )
    mock_api.message_responses.append((200, body, RATE_HEADERS))
    llm, ledger, _ = build(tmp_path, mock_api, fake_clock)
    r = await llm.call("k", model=M, max_tokens=64, messages=MSGS)
    assert r.refusal and r.refusal_category == "cyber" and r.refusal_explanation == "declined"
    assert r.text == ""
    assert ledger.n_lines == 1


async def test_refusal_without_stop_details(tmp_path, mock_api, fake_clock):
    mock_api.message_responses.append((200, message_body([], stop_reason="refusal"), RATE_HEADERS))
    llm, *_ = build(tmp_path, mock_api, fake_clock)
    r = await llm.call("k", model=M, max_tokens=64, messages=MSGS)
    assert r.refusal and r.refusal_category is None


async def test_429_retry_after_then_success(tmp_path, mock_api, fake_clock):
    mock_api.message_responses += [
        (429, error_body("rate_limit_error"), {"retry-after": "7"}),
        (200, message_body(), RATE_HEADERS),
    ]
    llm, ledger, limiter = build(tmp_path, mock_api, fake_clock)
    r = await llm.call("k", model=M, max_tokens=64, messages=MSGS)
    assert r.attempts == 2 and len(mock_api.message_calls()) == 2
    # the retry waited at least retry-after, and every worker was paused
    assert max(fake_clock.sleeps) >= 7.0
    assert limiter.paused_until >= 1000.0 + 7.0
    assert len(read_lines(tmp_path / "ledger.jsonl")) == 1
    assert ledger.outstanding_usd == 0


async def test_529_then_success(tmp_path, mock_api, fake_clock):
    mock_api.message_responses += [
        (529, error_body("overloaded_error"), {}),
        (500, error_body("api_error"), {}),
        (200, message_body(), RATE_HEADERS),
    ]
    llm, ledger, _ = build(tmp_path, mock_api, fake_clock)
    r = await llm.call("k", model=M, max_tokens=64, messages=MSGS)
    assert r.attempts == 3
    assert len(fake_clock.sleeps) == 2 and all(s > 0 for s in fake_clock.sleeps)
    assert ledger.n_lines == 1


async def test_400_not_retried(tmp_path, mock_api, fake_clock):
    mock_api.message_responses.append((400, error_body("invalid_request_error"), {}))
    llm, ledger, _ = build(tmp_path, mock_api, fake_clock)
    with pytest.raises(anthropic.BadRequestError):
        await llm.call("k", model=M, max_tokens=64, messages=MSGS)
    assert len(mock_api.message_calls()) == 1
    assert ledger.n_lines == 0 and ledger.outstanding_usd == 0


async def test_spend_cap_429_not_retried(tmp_path, mock_api, fake_clock):
    body = error_body("rate_limit_error", "monthly cap", {"error_code": "enforced_spend_limit_reached"})
    mock_api.message_responses.append((429, body, {}))
    llm, *_ = build(tmp_path, mock_api, fake_clock)
    with pytest.raises(anthropic.RateLimitError):
        await llm.call("k", model=M, max_tokens=64, messages=MSGS)
    assert len(mock_api.message_calls()) == 1


async def test_retries_exhausted(tmp_path, mock_api, fake_clock):
    mock_api.message_responses += [(529, error_body("overloaded_error"), {})] * 3
    llm, ledger, _ = build(tmp_path, mock_api, fake_clock, max_attempts=3)
    with pytest.raises(anthropic.OverloadedError):
        await llm.call("k", model=M, max_tokens=64, messages=MSGS)
    assert len(mock_api.message_calls()) == 3 and ledger.outstanding_usd == 0


async def test_connection_error_retried_and_charged_as_estimate(tmp_path, mock_api, fake_clock):
    def boom(request):
        raise httpx2.ConnectError("reset", request=request)

    mock_api.message_responses += [boom, (200, message_body(), RATE_HEADERS)]
    llm, ledger, _ = build(tmp_path, mock_api, fake_clock)
    r = await llm.call("k", model=M, max_tokens=64, messages=MSGS)
    assert r.attempts == 2
    lines = read_lines(tmp_path / "ledger.jsonl")
    assert len(lines) == 2 and lines[0]["estimated"] is True and lines[0]["error"] == "APIConnectionError"


async def test_budget_cap_blocks_before_any_request(tmp_path, mock_api, fake_clock):
    llm, ledger, _ = build(tmp_path, mock_api, fake_clock, cap=0.0001)
    with pytest.raises(BudgetExceeded):
        await llm.call("k", model=M, max_tokens=1_000, messages=MSGS)  # worst case > $0.0001
    assert mock_api.requests == []
    assert ledger.n_lines == 0


@pytest.mark.parametrize("param", ["temperature", "top_p", "top_k"])
async def test_sampling_params_rejected(tmp_path, mock_api, fake_clock, param):
    llm, *_ = build(tmp_path, mock_api, fake_clock)
    with pytest.raises(ValueError, match=param):
        await llm.call("k", model=M, max_tokens=64, messages=MSGS, **{param: 0.5})
    assert mock_api.requests == []


def test_input_estimate_is_conservative():
    text = "The quick brown fox jumps over the lazy dog. " * 100  # ~1,000 real tokens
    est = estimate_input_tokens({"messages": [{"role": "user", "content": text}]})
    assert est >= len(text) / 4


def test_input_estimate_counts_output_config_schema():
    base = {"model": M, "max_tokens": 10, "messages": MSGS}
    schema = {"type": "json_schema", "schema": {"type": "object", "properties": {f"f{i}": {"type": "string"} for i in range(50)}}}
    with_schema = {**base, "output_config": {"effort": "low", "format": schema}}
    assert estimate_input_tokens(with_schema) > estimate_input_tokens(base) + 100
    # model and max_tokens are not prompt text
    assert estimate_input_tokens(base) == estimate_input_tokens({**base, "model": "x" * 500, "max_tokens": 10**6})


# ---------------------------------------------------------------- cancellation mid-request
async def test_cancel_while_request_in_flight_is_charged(tmp_path, fake_clock):
    import asyncio

    received = asyncio.Event()
    never = asyncio.Event()

    async def handler(request):
        received.set()
        await never.wait()  # the API is still generating when the user presses Ctrl-C
        raise AssertionError("unreachable")

    http = anthropic.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(handler))
    api = anthropic.AsyncAnthropic(api_key="test-key", http_client=http, max_retries=0)
    ledger = CostLedger(tmp_path / "ledger.jsonl", hard_cap_usd=22.0)
    limiter = RateLimiter(1_000, 500_000, 80_000, clock=fake_clock, sleep=fake_clock.sleep)
    llm = LLMClient(ledger, limiter, api, sleep=fake_clock.sleep)
    task = asyncio.create_task(llm.call("k", model=M, max_tokens=64, messages=MSGS))
    await asyncio.wait_for(received.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    lines = read_lines(tmp_path / "ledger.jsonl")
    assert len(lines) == 1 and lines[0]["estimated"] is True and lines[0]["error"] == "CancelledError"
    assert lines[0]["cost_usd"] > 0 and ledger.spent_usd == pytest.approx(lines[0]["cost_usd"])
    assert ledger.outstanding_usd == 0


async def test_cancel_before_sending_is_not_charged(tmp_path, mock_api, fake_clock):
    import asyncio

    in_limiter = asyncio.Event()

    async def stuck_sleep(dt):
        in_limiter.set()
        await asyncio.Event().wait()

    ledger = CostLedger(tmp_path / "ledger.jsonl", hard_cap_usd=22.0)
    limiter = RateLimiter(1_000, 500_000, 80_000, clock=fake_clock, sleep=stuck_sleep)
    llm = LLMClient(ledger, limiter, mock_api.client(), sleep=fake_clock.sleep)
    limiter.pause_for(3600)  # every worker waits in the rate limiter: nothing is sent
    task = asyncio.create_task(llm.call("k", model=M, max_tokens=64, messages=MSGS))
    await asyncio.wait_for(in_limiter.wait(), 5)
    assert ledger.outstanding_usd > 0  # reserved, waiting for the limiter
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert mock_api.requests == []
    assert ledger.n_lines == 0 and ledger.outstanding_usd == 0


# ---------------------------------------------------------------- response cache
async def test_response_cache_replays_finished_calls_for_free(tmp_path, mock_api, fake_clock):
    mock_api.message_responses.append((200, message_body([{"type": "text", "text": "round 1"}]), RATE_HEADERS))
    cache = tmp_path / "cache" / "calls.jsonl"
    llm, ledger, _ = build(tmp_path, mock_api, fake_clock, cache=cache)
    r1 = await llm.call("ep1/r1/a1", model=M, max_tokens=64, messages=MSGS)
    assert not r1.cached and ledger.n_lines == 1

    # the episode crashed later; a new process re-runs it from the start
    ledger.close()
    llm2, ledger2, _ = build(tmp_path, mock_api, fake_clock, cache=cache)
    r2 = await llm2.call("ep1/r1/a1", model=M, max_tokens=64, messages=MSGS)
    assert r2.cached and r2.text == "round 1" and r2.cost_usd == r1.cost_usd
    assert len(mock_api.message_calls()) == 1  # nothing sent again
    assert ledger2.n_lines == 1 and ledger2.outstanding_usd == 0  # nothing paid again


async def test_response_cache_key_reused_for_other_request(tmp_path, mock_api, fake_clock):
    mock_api.message_responses += [
        (200, message_body([{"type": "text", "text": "first"}]), RATE_HEADERS),
        (200, message_body([{"type": "text", "text": "second"}]), RATE_HEADERS),
    ]
    llm, ledger, _ = build(tmp_path, mock_api, fake_clock, cache=tmp_path / "c.jsonl")
    await llm.call("k", model=M, max_tokens=64, messages=MSGS)
    with pytest.warns(RuntimeWarning, match="different request"):
        r = await llm.call("k", model=M, max_tokens=64, messages=[{"role": "user", "content": "Other"}])
    assert r.text == "second" and not r.cached and ledger.n_lines == 2


async def test_no_cache_by_default(tmp_path, mock_api, fake_clock):
    mock_api.message_responses += [(200, message_body(), RATE_HEADERS)] * 2
    llm, ledger, _ = build(tmp_path, mock_api, fake_clock)
    await llm.call("k", model=M, max_tokens=64, messages=MSGS)
    await llm.call("k", model=M, max_tokens=64, messages=MSGS)
    assert len(mock_api.message_calls()) == 2 and ledger.n_lines == 2
