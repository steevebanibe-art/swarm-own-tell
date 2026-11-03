import asyncio

import pytest

from harness.budget import BudgetExceeded, CostLedger
from harness.jsonl_store import JsonlStore
from harness.runner import Progress, RunStats, run_tasks


async def test_resume_skips_done_keys(tmp_path):
    store = JsonlStore(tmp_path / "runs" / "r.jsonl")
    store.append({"key": "t0", "status": "ok"})
    store.append({"key": "t1", "status": "error", "error": "boom"})  # retried
    seen = []

    async def worker(task):
        seen.append(task["key"])
        return {"value": task["key"].upper()}

    tasks = [{"key": f"t{i}"} for i in range(5)]
    lines = []
    stats = await run_tasks(tasks, worker, store, concurrency=3, progress=lines.append)
    assert sorted(seen) == ["t1", "t2", "t3", "t4"]
    assert stats.skipped == 1 and stats.done == 4 and stats.stopped_reason == "complete"
    assert store.done_keys() == {f"t{i}" for i in range(5)}
    assert any("resuming" in l for l in lines)
    # running again does nothing
    seen.clear()
    await run_tasks(tasks, worker, store, concurrency=3, progress=False)
    assert seen == []


async def test_stops_on_budget_exceeded_and_finishes_in_flight(tmp_path):
    store = JsonlStore(tmp_path / "r.jsonl")
    ledger = CostLedger(tmp_path / "ledger.jsonl", hard_cap_usd=0.5)
    started = []

    async def worker(task):
        started.append(task["key"])
        r = ledger.reserve(0.1)  # raises once the cap is reached
        await asyncio.sleep(0.01)
        ledger.settle(r, key=task["key"], model="m", usage=None, cost_usd=0.1)
        return {}

    tasks = [{"key": f"t{i:02d}"} for i in range(30)]
    stats = await run_tasks(tasks, worker, store, concurrency=4, ledger=ledger, progress=False)
    assert stats.stopped_reason == "budget"
    assert stats.done == 5  # 5 x $0.10 fit under the $0.50 cap
    assert len(store.done_keys()) == 5
    assert ledger.outstanding_usd == 0  # in-flight tasks were settled, not abandoned
    assert len(started) < 30  # no new tasks after the stop


async def test_worker_errors_are_recorded(tmp_path):
    store = JsonlStore(tmp_path / "r.jsonl")

    async def worker(task):
        if task["key"] == "bad":
            raise ValueError("nope")
        return {"x": 1}

    stats = await run_tasks([{"key": "ok"}, {"key": "bad"}], worker, store, progress=False)
    assert stats.done == 1 and stats.failed == 1
    recs = {r["key"]: r for r in store.iter_records()}
    assert recs["bad"]["status"] == "error" and "nope" in recs["bad"]["error"]


async def test_cancellation_keeps_store_consistent(tmp_path):
    store = JsonlStore(tmp_path / "r.jsonl")
    gate = asyncio.Event()

    async def worker(task):
        if task["key"] != "t0":
            await gate.wait()
        return {}

    run = asyncio.create_task(run_tasks([{"key": f"t{i}"} for i in range(4)], worker, store, concurrency=4, progress=False))
    for _ in range(20):
        await asyncio.sleep(0)
    run.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run
    assert store.done_keys() == {"t0"}


def test_duplicate_keys_rejected(tmp_path):
    store = JsonlStore(tmp_path / "r.jsonl")

    async def worker(task):
        return {}

    with pytest.raises(ValueError):
        asyncio.run(run_tasks([{"key": "a"}, {"key": "a"}], worker, store, progress=False))


def test_progress_line(tmp_path, fake_clock):
    ledger = CostLedger(tmp_path / "l.jsonl", hard_cap_usd=10)
    stats = RunStats(total=100, started=fake_clock())
    prog = Progress(stats, ledger, window_s=300, clock=fake_clock)
    fake_clock.t += 60
    for _ in range(20):
        stats.done += 1
        stats.finish_times.append(fake_clock())
        ledger.settle(None, key="x", model="m", usage=None, cost_usd=0.05)
    line = prog.line()
    assert "20/100 done" in line
    assert "20.0/min" in line
    assert "ETA 4m00s" in line
    assert "spent $1.000" in line
    assert "projected $5.00" in line


async def test_store_failure_cancels_other_workers(tmp_path):
    class FailingStore(JsonlStore):
        def append(self, record):
            if record.get("key") == "t0":
                raise OSError("disk full")
            super().append(record)

    store = FailingStore(tmp_path / "r.jsonl")
    gate = asyncio.Event()
    cancelled = []

    async def worker(task):
        if task["key"] != "t0":
            try:
                await gate.wait()
            except asyncio.CancelledError:
                cancelled.append(task["key"])
                raise
        return {}

    with pytest.raises(OSError, match="disk full"):
        await run_tasks([{"key": f"t{i}"} for i in range(3)], worker, store, concurrency=3, progress=False)
    assert sorted(cancelled) == ["t1", "t2"]


async def test_budget_stop_through_taskgroup_exception_group(tmp_path):
    store = JsonlStore(tmp_path / "r.jsonl")
    ledger = CostLedger(tmp_path / "ledger.jsonl", hard_cap_usd=0.25)
    started = []

    async def agent_call(name):
        r = ledger.reserve(0.05)
        await asyncio.sleep(0)
        ledger.settle(r, key=name, model="m", usage=None, cost_usd=0.05)

    async def worker(task):  # one round: 3 agents in parallel, the idiomatic 3.11+ way
        started.append(task["key"])
        async with asyncio.TaskGroup() as tg:
            for i in range(3):
                tg.create_task(agent_call(f"{task['key']}/a{i}"))
        return {}

    tasks = [{"key": f"t{i}"} for i in range(10)]
    stats = await run_tasks(tasks, worker, store, concurrency=1, ledger=ledger, progress=False)
    assert stats.stopped_reason == "budget"
    assert stats.failed == 0 and stats.done == 1
    assert len(started) == 2  # nothing starts after the cap is reached
    assert [r["status"] for r in store.iter_records()] == ["ok"]


async def test_nested_group_with_ordinary_errors_is_recorded(tmp_path):
    store = JsonlStore(tmp_path / "r.jsonl")

    async def worker(task):
        raise ExceptionGroup("round", [ValueError("bad agent"), ExceptionGroup("inner", [KeyError("x")])])

    stats = await run_tasks([{"key": "a"}, {"key": "b"}], worker, store, progress=False)
    assert stats.failed == 2 and stats.stopped_reason == "complete"
    assert all(r["status"] == "error" and "ExceptionGroup" in r["error"] for r in store.iter_records())


async def test_nested_budget_group_is_found(tmp_path):
    store = JsonlStore(tmp_path / "r.jsonl")

    async def worker(task):
        raise ExceptionGroup("round", [ValueError("x"), ExceptionGroup("inner", [BudgetExceeded(1, 0, 0, 1)])])

    stats = await run_tasks([{"key": f"t{i}"} for i in range(5)], worker, store, concurrency=1, progress=False)
    assert stats.stopped_reason == "budget" and stats.failed == 0
    assert list(store.iter_records()) == []


async def test_keyboard_interrupt_inside_group_is_not_swallowed(tmp_path):
    store = JsonlStore(tmp_path / "r.jsonl")

    async def worker(task):
        raise BaseExceptionGroup("round", [KeyboardInterrupt(), BudgetExceeded(1, 0, 0, 1)])

    with pytest.raises(BaseExceptionGroup):
        await run_tasks([{"key": "a"}], worker, store, progress=False)
    assert list(store.iter_records()) == []


async def test_worker_returning_its_own_key_is_still_resumed_by_task_key(tmp_path):
    store = JsonlStore(tmp_path / "r.jsonl")
    calls = []

    async def worker(task):  # e.g. a worker that returns its last CallResult.to_record()
        calls.append(task["key"])
        return {"key": f"{task['key']}/r12/a3", "text": "x"}

    tasks = [{"key": f"ep{i}"} for i in range(3)]
    await run_tasks(tasks, worker, store, progress=False)
    stats = await run_tasks(tasks, worker, store, progress=False)  # resume
    assert stats.skipped == 3 and stats.done == 0
    assert len(calls) == 3  # nothing re-run, nothing paid twice
    assert store.done_keys() == {"ep0", "ep1", "ep2"}
    assert {r["worker_key"] for r in store.iter_records()} == {"ep0/r12/a3", "ep1/r12/a3", "ep2/r12/a3"}
