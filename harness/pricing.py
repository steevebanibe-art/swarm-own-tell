"""Price table and cost computation for Claude API calls.

Prices are in USD per million tokens. Source: the official claude-api skill
(Claude Haiku 5.5: 0.10 input / 0.50 output when the prompt is at most
100,000 tokens, 0.50 / 2.50 above). Cache reads cost 0.1x the input price,
5-minute cache writes 1.25x, 1-hour cache writes 2x. The Message Batches API
halves every price.

To add a model, add one entry to ``PRICES``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

CACHE_READ_MULTIPLIER = 0.1
CACHE_WRITE_5M_MULTIPLIER = 1.25
CACHE_WRITE_1H_MULTIPLIER = 2.0
BATCH_DISCOUNT = 0.5


@dataclass(frozen=True)
class ModelPrice:
    """USD per million tokens, with an optional long-prompt tier."""

    input: float
    output: float
    long_input: float | None = None
    long_output: float | None = None
    long_threshold: int | None = None  # prompt tokens above which the long tier applies

    def rates(self, prompt_tokens: int) -> tuple[float, float]:
        """Return (input, output) USD per million tokens for a prompt of this size."""
        if (
            self.long_threshold is not None
            and prompt_tokens > self.long_threshold
            and self.long_input is not None
            and self.long_output is not None
        ):
            return self.long_input, self.long_output
        return self.input, self.output


PRICES: dict[str, ModelPrice] = {
    "claude-haiku-5-5": ModelPrice(
        input=0.10, output=0.50, long_input=0.50, long_output=2.50, long_threshold=100_000
    ),
}

USAGE_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)


def get_price(model: str) -> ModelPrice:
    """Look up a model, raising a clear KeyError when it is not in the table."""
    try:
        return PRICES[model]
    except KeyError:
        known = ", ".join(sorted(PRICES)) or "(none)"
        raise KeyError(
            f"No price for model {model!r}. Add it to harness.pricing.PRICES. Known models: {known}"
        ) from None


def usage_to_dict(usage: Any) -> dict[str, Any]:
    """Normalise an SDK Usage object (or a dict) into a plain dict of ints.

    Keeps the four standard counters (missing or None -> 0) and, when present,
    the ``cache_creation`` breakdown by TTL.
    """
    if usage is None:
        return {f: 0 for f in USAGE_FIELDS}
    if not isinstance(usage, Mapping):
        if hasattr(usage, "model_dump"):
            usage = usage.model_dump()
        else:
            usage = {f: getattr(usage, f, None) for f in (*USAGE_FIELDS, "cache_creation")}
    out: dict[str, Any] = {f: int(usage.get(f) or 0) for f in USAGE_FIELDS}
    cc = usage.get("cache_creation")
    if cc:
        if not isinstance(cc, Mapping):
            cc = cc.model_dump() if hasattr(cc, "model_dump") else vars(cc)
        out["cache_creation"] = {
            "ephemeral_5m_input_tokens": int(cc.get("ephemeral_5m_input_tokens") or 0),
            "ephemeral_1h_input_tokens": int(cc.get("ephemeral_1h_input_tokens") or 0),
        }
    return out


def prompt_tokens(usage: Mapping[str, Any]) -> int:
    """Total prompt size = uncached input + cache reads + cache writes."""
    return (
        int(usage.get("input_tokens") or 0)
        + int(usage.get("cache_read_input_tokens") or 0)
        + int(usage.get("cache_creation_input_tokens") or 0)
    )


def cost_usd(model: str, usage: Any, batch: bool = False) -> float:
    """Exact cost in USD of one call, from its usage.

    The long-prompt tier applies to every token of the call when the prompt
    (input + cache read + cache write tokens) exceeds the model's threshold.
    Cache writes without a TTL breakdown are priced as 5-minute writes.
    """
    price = get_price(model)
    u = usage_to_dict(usage)
    in_rate, out_rate = price.rates(prompt_tokens(u))

    write_total = u["cache_creation_input_tokens"]
    breakdown = u.get("cache_creation")
    if breakdown:
        w1h = breakdown["ephemeral_1h_input_tokens"]
        w5m = breakdown["ephemeral_5m_input_tokens"]
        # Any tokens not covered by the breakdown are treated as 5-minute writes.
        w5m += max(0, write_total - w1h - w5m)
    else:
        w1h, w5m = 0, write_total

    dollars_per_million = (
        u["input_tokens"] * in_rate
        + u["cache_read_input_tokens"] * in_rate * CACHE_READ_MULTIPLIER
        + w5m * in_rate * CACHE_WRITE_5M_MULTIPLIER
        + w1h * in_rate * CACHE_WRITE_1H_MULTIPLIER
        + u["output_tokens"] * out_rate
    )
    cost = dollars_per_million / 1_000_000
    return cost * BATCH_DISCOUNT if batch else cost


def worst_case_cost_usd(
    model: str,
    est_input_tokens: int,
    max_tokens: int,
    batch: bool = False,
    input_multiplier: float = 1.0,
) -> float:
    """Upper bound on the cost of one call before it is sent.

    All estimated input tokens are priced at the tier the estimate falls in,
    times ``input_multiplier`` (pass 1.25 or 2.0 when the request writes to the
    prompt cache), and the output is assumed to use all of ``max_tokens``.
    """
    price = get_price(model)
    in_rate, out_rate = price.rates(int(est_input_tokens))
    mult = max(1.0, float(input_multiplier))
    cost = (est_input_tokens * in_rate * mult + max_tokens * out_rate) / 1_000_000
    return cost * BATCH_DISCOUNT if batch else cost
