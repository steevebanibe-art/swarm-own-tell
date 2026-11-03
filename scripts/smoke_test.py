"""One tiny real call to check the key, the model id, pricing and rate-limit headers.

Cost: a few hundred-thousandths of a dollar (max_tokens=16, claude-haiku-5-5).
The API key is read from the environment by the SDK; it is never printed.

    set PYTHONUTF8=1
    .venv\\Scripts\\python.exe scripts\\smoke_test.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from harness.budget import CostLedger  # noqa: E402
from harness.client import LLMClient  # noqa: E402
from harness.ratelimit import RateLimiter  # noqa: E402

MODEL = "claude-haiku-5-5"


async def main() -> int:
    if "ANTHROPIC_API_KEY" not in os.environ:  # presence check only; the value is never read here
        print("ANTHROPIC_API_KEY is not set in this terminal. Set it, then run again.")
        return 2
    ledger = CostLedger(ROOT / "state" / "smoke_ledger.jsonl", hard_cap_usd=0.05)
    llm = LLMClient(ledger, RateLimiter(), max_concurrency=1, max_attempts=3)
    try:
        r = await llm.call(
            "smoke-test",
            model=MODEL,
            max_tokens=16,
            output_config={"effort": "low"},
            thinking={"type": "disabled"},
            messages=[{"role": "user", "content": "Hello"}],
        )
    finally:
        await llm.aclose()
    print("stop_reason :", r.stop_reason, "(refusal)" if r.refusal else "")
    print("text        :", repr(r.text))
    print("usage       :", json.dumps(r.usage))
    print(f"cost        : ${r.cost_usd:.8f}")
    print(f"latency     : {r.latency_s:.2f}s, attempts: {r.attempts}, request_id: {r.request_id}")
    print("rate limits :")
    for k, v in sorted(r.ratelimit.items()):
        print(f"  {k}: {v}")
    print("ledger      :", ledger.summary())
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
