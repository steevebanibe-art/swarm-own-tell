"""Score every logged CacheBoard item with the frozen judge (prompts/judge.md; Notes §1, §18).

Written during the hackathon. One Haiku call per item, `max_tokens` 32, low effort, thinking off,
cached system prompt. `wait()` items get score 0 from the harness (no call). Invalid replies,
refusals and prose declines are not items. The judge sees the masked copy of the action
(`judge_action`, D-6); `--unmasked` rescoring of tagged items is the sensitivity analysis.

    .venv\\Scripts\\python.exe scripts\\score_judge.py --set pilot
    .venv\\Scripts\\python.exe scripts\\score_judge.py --set grid --max-usd 8

Uses its own ledger (``state/judge_ledger.jsonl``) because the grid process holds the main one.
"""

from __future__ import annotations

import argparse
import asyncio
import gzip
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cacheboard import prompts as P  # noqa: E402
from cacheboard.episode import MODEL  # noqa: E402
from harness.budget import CostLedger  # noqa: E402
from harness.client import LLMClient, cached_system, is_network_error  # noqa: E402
from harness.jsonl_store import JsonlStore  # noqa: E402
from harness.ratelimit import RateLimiter  # noqa: E402
from harness.runner import run_tasks  # noqa: E402

JUDGE_MAX_TOKENS = 32
SCORE_RE = re.compile(r'"?score"?\s*:\s*"?(\d{1,3})')
JUDGE_CAP = 10.0


def parse_score(text: str | None) -> int | None:
    m = SCORE_RE.search(text or "")
    if not m:
        return None
    v = int(m.group(1))
    return v if 0 <= v <= 100 else None


def is_pilot(path: Path) -> bool:
    return int(re.search(r"-k(\d{4})", path.name).group(1)) > 1000


def load_items(paths: list[Path], unmasked: bool = False) -> list[dict[str, Any]]:
    """One task per item. With ``unmasked``, only items whose action contained a tag, unmasked."""
    tasks = []
    for path in paths:
        d = json.loads(gzip.open(path, "rt", encoding="utf-8").read())
        meta = d["meta"]
        pz = {p["agent"]: p for p in meta["puzzles"]}
        tags = [t for t in (meta.get("token"), meta.get("decoy_tag")) if t]
        for a in d["actions"]:
            if a["kind"] != "item":
                continue
            raw = json.dumps({"tool": a["tool"], "args": a["args"]}, ensure_ascii=False)
            if unmasked:
                if not any(t.lower() in raw.lower() for t in tags):
                    continue
                action_json = raw
            else:
                action_json = a["judge_action"]
            key = f"{meta['run_id']}/r{a['round']:02d}/{a['agent']}"
            tasks.append(
                {
                    "key": key,
                    "run_id": meta["run_id"],
                    "round": a["round"],
                    "agent": a["agent"],
                    "tool": a["log_tool"],
                    "wait": a["tool"] == "wait",
                    "user": P.judge_user(pz[a["agent"]]["text"], ", ".join(pz[a["agent"]]["keys"]), action_json),
                }
            )
    return tasks


async def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--set", required=True, choices=("pilot", "grid"))
    ap.add_argument("--unmasked", action="store_true", help="sensitivity: rescore tagged items without masking")
    ap.add_argument("--max-usd", type=float, default=None)
    ap.add_argument("--concurrency", type=int, default=12, help="API calls in flight")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    out = Path.cwd()
    paths = sorted(p for p in (out / "episodes").glob("*.json.gz") if is_pilot(p) == (args.set == "pilot"))
    name = f"judge_{args.set}" + ("_unmasked" if args.unmasked else "")
    store = JsonlStore(out / "scores" / f"{name}.jsonl")
    done = store.done_keys()
    tasks = [t for t in load_items(paths, args.unmasked) if t["key"] not in done]
    calls = sum(not t["wait"] for t in tasks)
    print(f"[judge] {name}: {len(paths)} episodes, {len(tasks)} items to score, {calls} judge calls", flush=True)
    if args.dry_run:
        print(P.judge_system()[:200], "...")
        print(tasks[0]["user"] if tasks else "")
        return 0
    if "ANTHROPIC_API_KEY" not in os.environ:
        print("ANTHROPIC_API_KEY is not set in this terminal.")
        return 2

    (out / "state").mkdir(exist_ok=True)
    ledger = CostLedger(out / "state" / "judge_ledger.jsonl", hard_cap_usd=JUDGE_CAP)
    start = ledger.spent_usd
    llm = LLMClient(
        ledger,
        RateLimiter(400, 200_000, 30_000),
        max_concurrency=args.concurrency,
        cache=out / "state" / f"call_cache_{name}.jsonl",
    )
    system = cached_system(P.judge_system())

    async def ask(key: str, user: str) -> Any:
        return await llm.call(
            key,
            model=MODEL,
            max_tokens=JUDGE_MAX_TOKENS,
            output_config={"effort": "low"},
            thinking={"type": "disabled"},
            system=system,
            messages=[{"role": "user", "content": user}],
        )

    async def worker(t: dict[str, Any]) -> dict[str, Any]:
        rec = {k: t[k] for k in ("key", "run_id", "round", "agent", "tool")}
        if t["wait"]:
            return rec | {"score": 0, "source": "harness", "cost_usd": 0.0}
        if args.max_usd is not None and ledger.spent_usd - start >= args.max_usd:
            raise RuntimeError("judge --max-usd reached")
        cost, texts = 0.0, []
        score = None
        for attempt in range(2):  # one retry if no integer 0-100 (judge.md, Parsing)
            r = await ask(f"{name}/{t['key']}" + ("/retry" if attempt else ""), t["user"])
            cost += r.cost_usd or 0.0
            texts.append(r.text)
            score = None if r.refusal else parse_score(r.text)
            if score is not None:
                break
        return rec | {"score": score, "source": "judge", "unscored": score is None, "text": texts[-1], "cost_usd": cost}

    try:
        stats = await run_tasks(
            tasks,
            worker,
            store,
            concurrency=args.concurrency * 2,
            key=lambda t: t["key"],
            ledger=ledger,
            status_file=out / "state" / f"status_{name}.json",
            transient=is_network_error,
        )
    finally:
        await llm.aclose()
    print(f"[done] {stats.done} done, {stats.failed} failed, stopped: {stats.stopped_reason}", flush=True)
    print(f"[budget] judge this process {ledger.spent_usd - start:.4f} USD, judge total {ledger.spent_usd:.4f} USD", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
