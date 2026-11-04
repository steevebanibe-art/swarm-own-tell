PRE-EXISTING GENERIC INFRASTRUCTURE - written on 3 Nov 2026, before the Swarm Alignment Hackathon (6-8 Nov 2026), and declared in the report. It contains nothing specific to the research project.

# harness: safe plumbing for many Claude API calls

No testbed, agent logic, monitor, statistic or prompt lives here. The package only
makes large numbers of API calls affordable, resumable and polite to rate limits.

| Module | What it does |
|---|---|
| `pricing.py` | Price table (`PRICES`, keyed by model id), `cost_usd(model, usage, batch=False)`, `worst_case_cost_usd(...)`. Long-prompt tier above 100,000 prompt tokens (input + cache read + cache write). |
| `budget.py` | `CostLedger`: append-only JSONL ledger, replayed on start-up; `reserve()` / `settle()` / `release()`; `BudgetExceeded` above the hard cap (default 22.00 USD). One process per ledger file (OS lock on `<ledger>.lock`, `LedgerLocked` for a second process); objects on the same file in one process share one state. |
| `ratelimit.py` | `RateLimiter`: continuous-refill token buckets for requests, input tokens and output tokens per minute; reconcile with real usage; slows down from response headers; global pause after a 429 with `retry-after`. |
| `client.py` | `LLMClient.call(key, model=..., max_tokens=..., messages=..., **params)` -> `CallResult`. `cached_system(text, ttl=None)` returns a system block with `cache_control` (ttl None/"5m"/"1h"; a prefix below the model's minimum cacheable length is silently not cached, so check `cache_read_input_tokens`); `is_network_error(exc)` for the runner's `transient=`. Concurrency cap, budget reservation, rate limiter, tenacity retries (429, 5xx incl. 529, network, timeout). Rejects `temperature`, `top_p`, `top_k`. A call cancelled, timed out or cut off while it may be in flight is charged at its worst case; a call that could not even open a connection is not charged. Optional response cache (`cache=`) keyed by call key + request hash. |
| `jsonl_store.py` | `JsonlStore`: fsync'd appends under a short cross-process lock (`<file>.append-lock`, so parallel processes never overwrite each other's lines), `done_keys()`, truncated-line repair, `export_gz()`; `prompt_hash(system, messages)`. |
| `filelock.py` | OS file locks (Windows `msvcrt`, POSIX `flock`): `ProcessFileLock` (one process per file) and `AppendLock` (short blocking lock around one write). |
| `runner.py` | `run_tasks(tasks, worker, store, concurrency)`: resume by key (the stored `key` is always the task key; a different `key` returned by the worker is kept as `worker_key`), progress lines (rate, ETA, spend, projected cost), clean stop on budget cap (also when wrapped in an `ExceptionGroup` from `asyncio.TaskGroup`) or Ctrl-C. Added the same day: `priority=` (start order, stable), `stop_file=` (no new starts while the file exists: stop, or pause with `stop_file_wait=True`), `status_file=` (JSON status replaced atomically every `status_every_s`, default 10 min), network-down pause (`transient=`, e.g. `client.is_network_error`: such failures are requeued, and when more than half of the outcomes of the last 2 min are transient failures new starts pause 60 s, then one probe task tests the network). |
| `jsonparse.py` | `parse_reply(content, stop_reason)` -> `ParsedReply`: text blocks only; `strict` (the whole text is one JSON object) and `lenient` (after removing code fences, or the first balanced `{...}` that parses, string-aware) logged separately; `max_tokens` and `refusal` stops are invalid under both. |
| `batch.py` | `BatchRunner`: Message Batches submit (chunked, worst case reserved), resumable state file, poll, collect results by `custom_id`, costs settled at the batch price. A chunk is written as *pending* before `create`; a lost response is resolved by listing the server's batches in a bounded time window and checking the candidate's custom ids before anything is settled (adopt; wait if ambiguous; drop if absent). One process per state file (OS lock on `<state>.lock`, `BatchStateLocked` for a second process). On reload only the unsettled part of each batch is re-reserved. |

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
                    cache="state/call_cache_example.jsonl")  # optional, one per process: interrupted tasks replay finished calls
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
* Same for batches: one process per batch state file (a second process gets
  `BatchStateLocked`), and custom ids unique across every process that shares
  the API key (e.g. prefixed with the run name), because a lost `create` is
  matched against all the batches listed on the key.
* Results stores and response caches may be shared by processes without losing
  lines, but each process only knows the keys it loaded at start-up, so give
  each parallel process its own results file and its own cache file (e.g.
  `runs/n10.jsonl`, `state/call_cache_n10.jsonl`).
* Response cache: call keys must name one specific call (e.g.
  `episode/round/agent`). The cache returns a stored answer only when the key
  and the full request both match; otherwise it warns and sends the call.
* Run the tests with `.venv\Scripts\python.exe -m pytest` (fully offline).
* `scripts/hf_launch.py` (4 Nov 2026, same status): runs a command of this repository as a Hugging Face Job. Code = git-tracked files mounted read-only at `/code`; work folder (state, runs) = a private bucket folder mounted read-write at `/work`; the bucket's privacy is checked before any upload; explicit timeout required; Anthropic key only with `--with-anthropic-key`, as an encrypted Job secret. `selftest` runs this test suite in a Job with its temporary folders on the bucket.
