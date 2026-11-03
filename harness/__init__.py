"""Generic plumbing for running many Claude API calls safely.

Pre-existing generic infrastructure (see harness/README.md). Nothing in this
package is specific to any research question: it only handles pricing, a hard
budget cap, client-side rate limiting, retries, durable JSONL storage, resumable
task running and the Message Batches API.
"""

from harness.budget import BudgetExceeded, CostLedger, LedgerLocked, Reservation
from harness.jsonl_store import JsonlStore, prompt_hash
from harness.pricing import PRICES, ModelPrice, cost_usd, worst_case_cost_usd
from harness.ratelimit import RateLimiter

__all__ = [
    "BudgetExceeded",
    "CostLedger",
    "LedgerLocked",
    "Reservation",
    "JsonlStore",
    "prompt_hash",
    "PRICES",
    "ModelPrice",
    "cost_usd",
    "worst_case_cost_usd",
    "RateLimiter",
]
