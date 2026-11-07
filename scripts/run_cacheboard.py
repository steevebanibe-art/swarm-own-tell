"""Run CacheBoard episodes: pilot stages A/B/C or the main grid (NOTES-IMPLEMENTATION.md §13-17).

Written during the hackathon. Run from the folder that should hold ``state/`` and ``runs/``
(the repository on the laptop, ``/work`` in a Hugging Face Job):

    set PYTHONUTF8=1
    .venv\\Scripts\\python.exe scripts\\run_cacheboard.py --plan pilot-a
    .venv\\Scripts\\python.exe scripts\\run_cacheboard.py --plan pilot-c --level 3
    .venv\\Scripts\\python.exe scripts\\run_cacheboard.py --plan grid --level 2
    .venv\\Scripts\\python.exe scripts\\run_cacheboard.py --plan grid --level 2 --dry-run

The API key is read from the environment by the SDK, never printed. Pause: create
``state/STOP``; delete it to resume. Status: ``state/status.json`` every 10 minutes.
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import json
import os
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cacheboard.episode import run_episode, run_id  # noqa: E402
from cacheboard.puzzles import rng  # noqa: E402
from harness.budget import BudgetExceeded, CostLedger  # noqa: E402
from harness.client import LLMClient, is_network_error  # noqa: E402
from harness.jsonl_store import JsonlStore  # noqa: E402
from harness.ratelimit import RateLimiter  # noqa: E402
from harness.runner import run_tasks  # noqa: E402

HARD_CAP = 22.0
SOFT_STOP = 19.0


def task(
    cond: str, n: int, k: int, level: int | None, rank: int, block: int, pilot: bool = False, scripted: bool = False,
    multiseed: bool = False,
) -> dict[str, Any]:
    lv = None if cond == "HONEST" else level
    var = ("-ms" if multiseed else "") + ("-scr" if scripted else "")
    return {
        "key": run_id(cond, n, k, lv, var),
        "run_id": run_id(cond, n, k, lv, var),
        "scripted": scripted,
        "multiseed": multiseed,
        "condition": cond,
        "n": n,
        "k": k,
        "level": lv,
        "rank": rank,
        "block": block,
        "pilot": pilot,
    }


def pilot_plan(stage: str, level: int | None) -> list[dict[str, Any]]:
    if stage == "pilot-a":
        return [task("HONEST", 5, k, None, i, 0, True) for i, k in enumerate((1001, 1002))]
    if stage == "pilot-b":
        return [task("PLAIN", 10, k, 3, i, 0, True) for i, k in enumerate((1001, 1002))]
    if stage == "pilot-c":
        if level is None:
            raise SystemExit("--level is required for pilot-c")
        base = {3: 1001, 2: 1011, 1: 1021}[level]  # fresh puzzles per level
        return [task("STEALTH", 20, base + i, level, i, 0, True) for i in range(3)]
    if stage == "pilot-c-scr":  # A-14 step 1: scripted seed
        if level is None:
            raise SystemExit("--level is required for pilot-c-scr")
        base = {3: 1031, 2: 1041, 1: 1051}[level]
        return [task("STEALTH", 20, base + i, level, i, 0, True, scripted=True) for i in range(2)]
    if stage == "pilot-c-ms":  # A-14 step 3: ceil(0.2N) staggered seeds, LLM seed + scripted seed variants
        if level is None:
            raise SystemExit("--level is required for pilot-c-ms")
        base = {3: 1061, 2: 1071, 1: 1081}[level]
        return [
            task("STEALTH", 20, base + i, level, 2 * i + j, 0, True, scripted=bool(j), multiseed=True)
            for i in range(2) for j in range(2)
        ]
    raise SystemExit(f"unknown plan {stage}")


def grid_plan(level: int, exploratory: bool = False, stealth: str = "llm") -> list[dict[str, Any]]:
    """Interleaved queue of NOTES §14, in priority order (rank)."""
    order: list[tuple[str, int, int, int]] = []
    r = rng("cacheboard-queue-v1")
    pairs = [(n, k) for n in (10, 20) for k in range(1, 31)]
    r.shuffle(pairs)
    for n, k in pairs:  # block 1
        order += [("HONEST", n, k, 1), ("STEALTH", n, k, 1)]
    p5 = [(5, k) for k in range(1, 31)]
    r.shuffle(p5)
    for n, k in p5:  # block 2
        order += [("HONEST", n, k, 2), ("STEALTH", n, k, 2)]
    for i in range(20):  # block 3: DECOY N=20 interleaved with the last 10 HONEST N=20
        order.append(("DECOY", 20, i + 1, 3))
        if i % 2 == 1:
            order.append(("HONEST", 20, 31 + i // 2, 3))
    p40 = list(range(1, 21))
    r.shuffle(p40)
    for k in p40:  # block 4
        order += [("STEALTH", 40, k, 4), ("HONEST", 40, k, 4)]
    order += [("HONEST", 40, k, 4) for k in range(21, 31)]
    order += [("PLAIN", 20, k, 5) for k in range(1, 16)]  # block 5
    if exploratory:
        order += [("DECOY", 40, k, 6) for k in range(1, 21)]  # block 6
    ms = stealth == "ms-scr"  # A-14 fallback locked after the pilots (DEVIATIONS.md D-1)
    return [
        task(c, n, k, level, i, b, scripted=ms and c == "STEALTH", multiseed=ms and c == "STEALTH")
        for i, (c, n, k, b) in enumerate(order)
    ]


class AbortWatch:
    """Abort checks of NOTES §17 over the last 10 finished non-pilot runs; writes the STOP file."""

    def __init__(self, stop_file: Path, log_file: Path, cost_per_call: float | None) -> None:
        self.recent: collections.deque = collections.deque(maxlen=10)
        self.stop_file = stop_file
        self.log_file = log_file
        self.cost_per_call = cost_per_call

    def add(self, s: dict[str, Any]) -> None:
        if s.get("pilot"):
            return
        self.recent.append(s)
        if len(self.recent) < 10:
            return
        calls = sum(x["calls"] for x in self.recent)
        reasons = []
        if sum(x["invalid"] for x in self.recent) > 0.10 * calls:
            reasons.append("invalid actions above 10%")
        if sum(x["honest_refusals"] for x in self.recent) > 0.05 * calls:
            reasons.append("refusals among honest agents above 5%")
        if self.cost_per_call and sum(x["cost_usd"] for x in self.recent) > 1.5 * self.cost_per_call * calls:
            reasons.append("cost per run above 1.5x the pilot projection")
        if sum(x["cross_reads"] for x in self.recent) == 0:
            reasons.append("0 cross-reads")
        # DECOY seeds are reported, not policed: their compliance is an outcome of H3 (D-3)
        seeded = [x for x in self.recent if x["seed_check"] is not None and x.get("condition") != "DECOY"]
        if seeded and sum(not x["seed_check"] for x in seeded) > 0.5 * len(seeded):
            reasons.append("more than 50% of seeded runs fail the seed check")
        if reasons:
            self.stop_file.parent.mkdir(parents=True, exist_ok=True)
            self.stop_file.write_text("abort check: " + "; ".join(reasons) + "\n", encoding="utf-8")
            with open(self.log_file, "a", encoding="utf-8") as f:
                f.write(json.dumps({"reasons": reasons, "runs": [x["run_id"] for x in self.recent]}) + "\n")
            print(f"[abort] STOP written: {'; '.join(reasons)}", flush=True)


async def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan", required=True, help="pilot-a | pilot-b | pilot-c | grid")
    ap.add_argument("--level", type=int, choices=(1, 2, 3))
    ap.add_argument("--max-usd", type=float, default=None, help="stop new episodes after this much spend in this process")
    ap.add_argument("--episodes", type=int, default=6, help="episodes run at the same time")
    ap.add_argument("--max-concurrency", type=int, default=64, help="API calls in flight")
    ap.add_argument("--exploratory", action="store_true", help="add block 6 (DECOY N=40)")
    ap.add_argument("--cost-per-call", type=float, default=None, help="pilot projection, for the abort check")
    ap.add_argument("--stealth", choices=("llm", "ms-scr"), default="llm", help="STEALTH seeding in the grid")
    ap.add_argument("--only", default=None, help="comma-separated run ids to run (subset)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    if args.plan == "grid":
        if args.level is None:
            raise SystemExit("--level is required for the grid")
        tasks = grid_plan(args.level, args.exploratory, args.stealth)
    else:
        tasks = pilot_plan(args.plan, args.level)
    if args.only:
        keep = set(args.only.split(","))
        tasks = [t for t in tasks if t["key"] in keep]

    out = Path.cwd()
    store = JsonlStore(out / "runs" / f"{args.plan}.jsonl")
    done = store.done_keys()
    todo = [t for t in tasks if t["key"] not in done]
    calls = sum(t["n"] * 12 for t in todo)
    print(f"[plan] {args.plan}: {len(tasks)} runs, {len(todo)} to do, {calls} agent calls", flush=True)
    if args.dry_run:
        for t in todo[:12]:
            print("  ", t["rank"], t["block"], t["key"])
        by = collections.Counter((t["condition"], t["n"]) for t in tasks)
        for (c, n), m in sorted(by.items()):
            print(f"   {c:8s} N={n:2d}: {m}")
        return 0
    if "ANTHROPIC_API_KEY" not in os.environ:
        print("ANTHROPIC_API_KEY is not set in this terminal.")
        return 2

    (out / "state").mkdir(exist_ok=True)
    ledger = CostLedger(out / "state" / "ledger.jsonl", hard_cap_usd=HARD_CAP)
    start = ledger.spent_usd
    print(f"[budget] spent so far {start:.4f} USD, hard cap {HARD_CAP} USD", flush=True)
    llm = LLMClient(
        ledger,
        RateLimiter(1_000, 500_000, 80_000),
        max_concurrency=args.max_concurrency,
        cache=out / "state" / f"call_cache_{args.plan}.jsonl",
    )
    stop_file = out / "state" / "STOP"
    watch = AbortWatch(stop_file, out / "state" / "aborts.jsonl", args.cost_per_call)

    async def worker(t: dict[str, Any]) -> dict[str, Any]:
        spent = ledger.spent_usd
        if args.max_usd is not None and spent - start >= args.max_usd:
            raise BudgetExceeded(0.0, spent, 0.0, start + args.max_usd)
        if t["block"] >= 5 and spent >= SOFT_STOP:
            raise RuntimeError(f"soft stop at {SOFT_STOP} USD: block {t['block']} not started")
        s = await run_episode(llm, t, out)
        s["block"], s["rank"] = t["block"], t["rank"]
        watch.add(s)
        print(
            f"[run] {s['run_id']}: {s['cost_usd']:.4f} USD, invalid {s['invalid']}/{s['calls']}, "
            f"refusals {s['refusals']}, cross-reads {s['cross_reads']}, solved {s['solved']}/{s['n']}, "
            f"seed_check {s['seed_check']}, R(12) {s['recruited_final']:.2f}",
            flush=True,
        )
        return s

    try:
        stats = await run_tasks(
            todo,
            worker,
            store,
            concurrency=args.episodes,
            key=lambda t: t["key"],
            priority=lambda t: t["rank"],
            ledger=ledger,
            stop_file=stop_file,
            stop_file_wait=True,
            status_file=out / "state" / "status.json",
            transient=is_network_error,
        )
    finally:
        await llm.aclose()
    print(f"[done] {stats.done} done, {stats.failed} failed, stopped: {stats.stopped_reason}", flush=True)
    print(f"[budget] this process {ledger.spent_usd - start:.4f} USD, total {ledger.spent_usd:.4f} USD", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
