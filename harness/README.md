PRE-EXISTING GENERIC INFRASTRUCTURE - written on 3 Nov 2026, before the Swarm Alignment Hackathon (6-8 Nov 2026), and declared in the report. It contains nothing specific to the research project.

# harness: safe plumbing for many Claude API calls

No testbed, agent logic, monitor, statistic or prompt lives here. The package only
makes large numbers of API calls affordable, resumable and polite to rate limits.

| Module | What it does |
|---|---|
| `pricing.py` | Price table (`PRICES`, keyed by model id), `cost_usd(model, usage, batch=False)`, `worst_case_cost_usd(...)`. Long-prompt tier above 100,000 prompt tokens (input + cache read + cache write). |
| `budget.py` | `CostLedger`: append-only JSONL ledger, replayed on start-up; `reserve()` / `settle()` / `release()`; `BudgetExceeded` above the hard cap (default 22.00 USD). One process per ledger file (OS lock on `<ledger>.lock`, `LedgerLocked` for a second process); objects on the same file in one process share one state. |
| `ratelimit.py` | `RateLimiter`: continuous-refill token buckets for requests, input tokens and output tokens per minute; reconcile with real usage; slows down from response headers; global pause after a 429 with `retry-after`. |
| `client.py` | `LLMClient.call(key, model=..., max_tokens=..., messages=..., **params)` -> `CallResult`. Concurrency cap, budget reservation, rate limiter, tenacity retries (429, 5xx incl. 529, network, timeout). Rejects `temperature`, `top_p`, `top_k`. A call cancelled while it may be in flight is charged at its worst case. Optional response cache (`cache=`) keyed by call key + request hash. |
| `jsonl_store.py` | `JsonlStore`: fsync'd appends, `done_keys()`, truncated-line repair, `export_gz()`; `prompt_hash(system, messages)`. |
| `runner.py` | `run_tasks(tasks, worker, store, concurrency)`: resume by key, progress lines (rate, ETA, spend, projected cost), clean stop on budget cap (also when wrapped in an `ExceptionGroup` from `asyncio.TaskGroup`) or Ctrl-C. |
| `batch.py` | `BatchRunner`: Message Batches submit (chunked, worst case reserved), resumable state file, poll, collect results by `custom_id`, costs settled at the batch price. A chunk is written as *pending* before `create`; a lost response is resolved by listing the server's batches (adopt, or drop if absent). On reload only the unsettled part of each batch is re-reserved. |

## Minimal use

```python
import asyncio
from harness.budget import CostLedger
from harness.client import LLMClient
from harness.jsonl_store import JsonlStore
from harness.ratelimit import RateLimiter
from harness.runner import run_tasks

async def main():
    ledger = CostLedger("state/ledger.jsonl", hard_cap_usd=22.0)
    llm = LLMClient(ledger, RateLimiter(1_000, 500_000, 80_000), max_concurrency=16,
                    cache="state/call_cache.jsonl")  # optional: interrupted tasks replay finished calls
    store = JsonlStore("runs/example.jsonl")

    async def worker(task):
        r = await llm.call(
            task["key"],
            model="claude-haiku-5-5",
            max_tokens=256,
            output_config={"effort": "low"},
            thinking={"type": "disabled"},
            messages=[{"role": "user", "content": task["text"]}],
        )
        return r.to_record()

    tasks = [{"key": f"t{i}", "text": "..."} for i in range(100)]
    await run_tasks(tasks, worker, store, concurrency=16, ledger=ledger)
    await llm.aclose()

asyncio.run(main())
```

Notes

* The API key is read from the environment by the SDK (`ANTHROPIC_API_KEY`); the
  harness never reads, prints or logs it.
* Output tokens per minute are counted by the API on tokens actually produced
  (`max_tokens` does not count). `LLMClient` still reserves `max_tokens` in the
  local OTPM bucket by default (safe); pass `output_reservation=` to reserve less.
  The difference is credited back after each call.
* Cache reads do not count toward input tokens per minute; cache writes do.
* One process per ledger file. A second process on the same file stops at once
  with `LedgerLocked`. To run several processes at the same time (e.g. one per
  fleet size), give each its own ledger file and its own slice of the cap.
* Response cache: call keys must name one specific call (e.g.
  `episode/round/agent`). The cache returns a stored answer only when the key
  and the full request both match; otherwise it warns and sends the call.
* Run the tests with `.venv\Scripts\python.exe -m pytest` (fully offline).
