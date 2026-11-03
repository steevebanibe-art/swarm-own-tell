"""Pricing rule (claude-api skill, Claude Haiku 5.5): two rate cards chosen by
prompt length. 0.10 / 0.50 USD per million input / output tokens when the
prompt is 100,000 tokens or fewer; 0.50 / 2.50 when it is longer. The prompt is
input + cache read + cache write tokens, and the chosen card prices every token
of the call. Cache reads 0.1x input, 5-minute writes 1.25x, 1-hour writes 2x;
the Message Batches API halves everything.

Short-tier tests keep the prompt at or below 100,000 tokens; long-tier tests
put it above. (An earlier draft used 1,000,000-token prompts and expected short
tier prices: that was a test bug, the code was right.)
"""

import pytest

from harness.pricing import cost_usd, worst_case_cost_usd

M = "claude-haiku-5-5"
SHORT_IN, SHORT_OUT = 0.10, 0.50
LONG_IN, LONG_OUT = 0.50, 2.50


# ----------------------------------------------------------------- short tier
def test_short_tier_basic_cost():
    u = {"input_tokens": 10_000, "output_tokens": 2_000}
    assert cost_usd(M, u) == pytest.approx((10_000 * SHORT_IN + 2_000 * SHORT_OUT) / 1e6)


def test_short_tier_batch_halves():
    u = {"input_tokens": 10_000, "output_tokens": 2_000}
    assert cost_usd(M, u, batch=True) == pytest.approx(cost_usd(M, u) / 2)


def test_short_tier_cache_write_without_breakdown_is_5m():
    u = {"input_tokens": 0, "output_tokens": 0, "cache_creation_input_tokens": 80_000}
    assert cost_usd(M, u) == pytest.approx(80_000 * SHORT_IN * 1.25 / 1e6)


def test_short_tier_cache_multipliers_and_breakdown():
    u = {
        "input_tokens": 1_000,
        "output_tokens": 0,
        "cache_read_input_tokens": 10_000,
        "cache_creation_input_tokens": 3_000,
        "cache_creation": {"ephemeral_5m_input_tokens": 1_000, "ephemeral_1h_input_tokens": 2_000},
    }
    expected = (1_000 * 0.10 + 10_000 * 0.01 + 1_000 * 0.125 + 2_000 * 0.20) / 1e6
    assert cost_usd(M, u) == pytest.approx(expected)


# ----------------------------------------------------------------- long tier
def test_long_tier_basic_cost():
    u = {"input_tokens": 1_000_000, "output_tokens": 1_000_000}
    assert cost_usd(M, u) == pytest.approx(LONG_IN + LONG_OUT)  # 3.00


def test_long_tier_batch_halves():
    u = {"input_tokens": 1_000_000, "output_tokens": 1_000_000}
    assert cost_usd(M, u, batch=True) == pytest.approx((LONG_IN + LONG_OUT) / 2)  # 1.50


def test_long_tier_cache_write_without_breakdown_is_5m():
    u = {"input_tokens": 0, "output_tokens": 0, "cache_creation_input_tokens": 1_000_000}
    assert cost_usd(M, u) == pytest.approx(LONG_IN * 1.25)  # 0.625


def test_long_tier_counts_cache_tokens():
    # 60k input + 50k cache read = 110k prompt tokens > 100k -> long tier for everything
    u = {"input_tokens": 60_000, "cache_read_input_tokens": 50_000, "output_tokens": 1_000}
    expected = (60_000 * 0.50 + 50_000 * 0.05 + 1_000 * 2.50) / 1e6
    assert cost_usd(M, u) == pytest.approx(expected)


# ----------------------------------------------------------------- boundary
def test_boundary_100000_is_short_100001_is_long():
    at = {"input_tokens": 100_000, "output_tokens": 1_000}
    above = {"input_tokens": 100_001, "output_tokens": 1_000}
    assert cost_usd(M, at) == pytest.approx((100_000 * SHORT_IN + 1_000 * SHORT_OUT) / 1e6)
    assert cost_usd(M, above) == pytest.approx((100_001 * LONG_IN + 1_000 * LONG_OUT) / 1e6)


def test_boundary_counts_cache_reads_and_writes():
    # 99,999 input + 1 cache read = 100,000 -> short; one more cache-write token -> long
    at = {"input_tokens": 99_999, "cache_read_input_tokens": 1, "output_tokens": 0}
    above = {**at, "cache_creation_input_tokens": 1}
    assert cost_usd(M, at) == pytest.approx((99_999 * SHORT_IN + 1 * SHORT_IN * 0.1) / 1e6)
    assert cost_usd(M, above) == pytest.approx((99_999 * LONG_IN + 1 * LONG_IN * 0.1 + 1 * LONG_IN * 1.25) / 1e6)


# ----------------------------------------------------------------- misc
def test_unknown_model_raises_clear_keyerror():
    with pytest.raises(KeyError, match="No price for model 'claude-nope'"):
        cost_usd("claude-nope", {"input_tokens": 1})


def test_usage_object_with_none_fields():
    from anthropic.types import Usage

    u = Usage(input_tokens=1_000, output_tokens=10, cache_read_input_tokens=None, cache_creation_input_tokens=None)
    assert cost_usd(M, u) == pytest.approx((1_000 * SHORT_IN + 10 * SHORT_OUT) / 1e6)


def test_worst_case_both_tiers_and_boundary():
    assert worst_case_cost_usd(M, 1_000, 500) == pytest.approx((1_000 * 0.10 + 500 * 0.50) / 1e6)
    assert worst_case_cost_usd(M, 100_000, 0) == pytest.approx(100_000 * SHORT_IN / 1e6)
    assert worst_case_cost_usd(M, 100_001, 10) == pytest.approx((100_001 * LONG_IN + 10 * LONG_OUT) / 1e6)
    assert worst_case_cost_usd(M, 200_000, 0) == pytest.approx(200_000 * 0.50 / 1e6)
    assert worst_case_cost_usd(M, 1_000, 0, input_multiplier=2.0) == pytest.approx(1_000 * 0.20 / 1e6)
    assert worst_case_cost_usd(M, 1_000, 500, batch=True) == pytest.approx((100 + 250) / 2 / 1e6)
