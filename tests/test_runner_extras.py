"""Priority order, stop file, status file and network-down pause of run_tasks (all offline)."""

import asyncio
import json

from harness.jsonl_store import JsonlStore
from harness.runner import run_tasks


class NetErr(Exception):
    """Stands for a connection error left after the client's retries."""


def is_net(exc):
    return isinstance(exc, NetErr)


async def test_priority_orders_starts_and_ties_keep_list_order(tmp_path):
    store = JsonlStore(tmp_path / "r.jsonl")
    order = []

    async def worker(task):
        order.append(task["key"])
        return {}

    tasks = [{"key": "a", "p": 2}, {"key": "b", "p": 1}, {"key": "c", "p": 2}, {"key": "d", "p": 0}, {"key": "e", "p": 1}]
    await run_tasks(tasks, worker, store, concurrency=1, priority=lambda t: t["p"], progress=False)
    assert order == ["d", "b", "e", "a", "c"]


async def test_without_priority_list_order_is_kept(tmp_path):
    store = JsonlStore(tmp_path / "r.jsonl")
    order = []

    async def worker(task):
        order.append(task["key"])
        return {}

    await run_tasks([{"key": k} for k in "qwerty"], worker, store, concurrency=1, progress=False)
    assert order == list("qwerty")


async def test_stop_file_stops_new_starts_and_resume_continues(tmp_path):
    store = JsonlStore(tmp_path / "r.jsonl")
    stop = tmp_path / "STOP"
    started = []

    async def worker(task):
        started.append(task["key"])
        if task["key"] == "t2":
            stop.write_text("pause", encoding="utf-8")  # e.g. an abort check fires
        await asyncio.sleep(0.01)
        return {}

    tasks = [{"key": f"t{i}"} for i in range(10)]
    lines = []
    stats = await run_tasks(tasks, worker, store, concurrency=2, stop_file=stop, progress=lines.append)
    assert stats.stopped_reason == "stop_file"
    assert len(started) <= 4  # in-flight tasks finish, nothing new starts
    assert store.done_keys() == set(started)  # in-flight tasks were written
    assert any("STOP" in l for l in lines)

    stop.unlink()
    stats = await run_tasks(tasks, worker, store, concurrency=2, stop_file=stop, progress=False)
    assert stats.stopped_reason == "complete"
    assert store.done_keys() == {t["key"] for t in tasks}


async def test_stop_file_wait_pauses_until_deleted(tmp_path):
    store = JsonlStore(tmp_path / "r.jsonl")
    stop = tmp_path / "STOP"
    stop.write_text("", encoding="utf-8")

    async def worker(task):
        return {}

    async def remove_later():
        await asyncio.sleep(0.1)
        stop.unlink()

    lines = []
    remover = asyncio.create_task(remove_later())
    stats = await run_tasks(
        [{"key": f"t{i}"} for i in range(3)],
        worker,
        store,
        stop_file=stop,
        stop_file_wait=True,
        stop_file_poll_s=0.01,
        progress=lines.append,
    )
    await remover
    assert stats.stopped_reason == "complete" and stats.done == 3
    assert any("paused" in l for l in lines) and any("resumed" in l for l in lines)


async def test_status_file_is_written_during_and_after_the_run(tmp_path):
    store = JsonlStore(tmp_path / "r.jsonl")
    status = tmp_path / "out" / "status.json"
    seen_mid_run = []

    async def worker(task):
        await asyncio.sleep(0.05)
        seen_mid_run.append(json.loads(status.read_text(encoding="utf-8")))
        return {}

    stats = await run_tasks(
        [{"key": f"t{i}"} for i in range(4)],
        worker,
        store,
        concurrency=1,
        status_file=status,
        status_every_s=0.01,
        progress=False,
    )
    assert stats.done == 4
    assert seen_mid_run and all("line" in s and "time_utc" in s for s in seen_mid_run)
    assert max(s["done"] for s in seen_mid_run) >= 1  # the heartbeat refreshed it while running
    final = json.loads(status.read_text(encoding="utf-8"))
    assert final["done"] == 4 and final["remaining"] == 0 and final["stopped_reason"] == "complete"
    assert not (tmp_path / "out" / "status.json.tmp").exists()


async def test_network_outage_pauses_then_resumes_without_errors(tmp_path):
    store = JsonlStore(tmp_path / "r.jsonl")
    net = {"down": True}
    sleeps = []

    async def fake_sleep(dt):
        sleeps.append(dt)
        net["down"] = False  # the network comes back during the pause
        await asyncio.sleep(0)

    async def worker(task):
        await asyncio.sleep(0)
        if net["down"]:
            raise NetErr("connection refused")
        return {}

    lines = []
    tasks = [{"key": f"t{i}"} for i in range(10)]
    stats = await run_tasks(
        tasks,
        worker,
        store,
        concurrency=4,
        transient=is_net,
        sleep=fake_sleep,
        progress=lines.append,
    )
    assert stats.stopped_reason == "complete"
    assert stats.done == 10 and stats.failed == 0
    assert stats.network_pauses >= 1 and stats.requeued >= 4
    assert 60.0 in sleeps
    assert all(r["status"] == "ok" for r in store.iter_records())
    assert any("pausing" in l for l in lines) and any("network back" in l for l in lines)


async def test_network_never_back_gives_up_and_writes_no_errors(tmp_path, fake_clock):
    store = JsonlStore(tmp_path / "r.jsonl")

    async def worker(task):
        raise NetErr("no route to host")

    stats = await run_tasks(
        [{"key": f"t{i}"} for i in range(5)],
        worker,
        store,
        concurrency=3,
        transient=is_net,
        clock=fake_clock,
        sleep=fake_clock.sleep,
        net_give_up_s=150,
        progress=False,
    )
    assert stats.stopped_reason == "network"
    assert stats.failed == 0 and store.done_keys() == set()
    assert not list(store.iter_records())  # outage failures are requeued, never written
    assert fake_clock.sleeps.count(60.0) == 3  # pause, probe, pause, probe, pause, then give up


async def test_isolated_transient_failure_is_requeued_then_recorded(tmp_path):
    store = JsonlStore(tmp_path / "r.jsonl")
    attempts = []

    async def worker(task):
        attempts.append(task["key"])
        if task["key"] == "bad":
            raise NetErr("reset by peer")
        return {}

    tasks = [{"key": f"t{i}"} for i in range(20)] + [{"key": "bad"}]
    stats = await run_tasks(tasks, worker, store, concurrency=2, transient=is_net, max_requeues=3, progress=False)
    assert attempts.count("bad") == 4  # first try + 3 requeues
    assert stats.failed == 1 and stats.requeued == 3 and stats.network_pauses == 0
    recs = {r["key"]: r for r in store.iter_records()}
    assert recs["bad"]["status"] == "error" and "NetErr" in recs["bad"]["error"]


async def test_transient_error_inside_exception_group_is_requeued(tmp_path):
    store = JsonlStore(tmp_path / "r.jsonl")
    calls = []

    async def worker(task):
        calls.append(task["key"])
        if len(calls) == 1:
            raise ExceptionGroup("taskgroup", [NetErr("timeout")])
        return {}

    stats = await run_tasks([{"key": "only"}], worker, store, transient=is_net, progress=False)
    assert calls == ["only", "only"]
    assert stats.done == 1 and stats.failed == 0 and stats.requeued == 1


async def test_non_transient_errors_are_recorded_as_before(tmp_path):
    store = JsonlStore(tmp_path / "r.jsonl")

    async def worker(task):
        raise ValueError("bad prompt")

    stats = await run_tasks([{"key": "x"}], worker, store, transient=is_net, progress=False)
    assert stats.failed == 1 and stats.requeued == 0
