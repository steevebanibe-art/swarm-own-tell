"""Resumable, bounded-concurrency task runner.

``run_tasks`` takes a list of tasks (each with a unique key), an async worker
and a ``JsonlStore``. Tasks whose key is already in the store are skipped, so
re-running the same command resumes where it stopped. Each finished task is
written immediately (one fsync'd line).

Order: tasks start in list order, or by ``priority(task)`` (lowest first; ties
keep list order). A requeued task keeps its place in that order.

Stopping:
* ``BudgetExceeded`` from any worker: no new task starts, tasks already in
  flight finish and are written, then the run returns with
  ``stopped_reason="budget"``. This also holds when the worker fans out its
  calls with ``asyncio.TaskGroup`` and the BudgetExceeded arrives wrapped in
  an ``ExceptionGroup`` (at any depth).
* ``stop_file``: when that file exists, no new task starts. With
  ``stop_file_wait=False`` (default) in-flight tasks finish and the run
  returns with ``stopped_reason="stop_file"`` (delete the file and re-run to
  resume). With ``stop_file_wait=True`` the queue pauses and starts again
  once the file is deleted (checked every ``stop_file_poll_s``).
* KeyboardInterrupt / cancellation: workers are cancelled, the store holds only
  complete lines, a summary is printed and the interruption is re-raised.
  Unfinished tasks run again on the next resume.
* Any other exception in a worker is written as ``{"key", "status": "error",
  "error"}`` (retried on resume, since ``done_keys`` ignores errors).
* An exception outside the worker (e.g. writing the store fails): the other
  workers are cancelled and the exception is re-raised
  (``stopped_reason="error"``).

Network-down pause (only when a ``transient`` predicate is given): a worker
exception for which ``transient(exc)`` is true (searched inside exception
groups too) is not written; the task goes back to the queue. When more than
``net_fail_ratio`` of the task outcomes of the last ``net_window_s`` seconds
(at least ``net_min_samples`` of them) are transient failures, no new task
starts for ``net_pause_s`` seconds; then exactly one task is started as a
probe. Its success (or any non-transient outcome) reopens the queue; a
transient failure pauses again. Failures seen during an outage do not count
against the task; outside an outage a task is requeued at most
``max_requeues`` times, then written as an error. ``net_give_up_s`` (default
None = never) stops the run with ``stopped_reason="network"`` after that
long without a healthy network.

Status file: with ``status_file``, a small JSON document (counts, rate, ETA,
spend, projection, pause state, last progress line, UTC time) is replaced
atomically every ``status_every_s`` seconds (default 600) and at the end, so a
phone or another process can read it at any time. A failed write is reported
on the progress channel and never stops the run.
"""

from __future__ import annotations

import asyncio
import collections
import datetime as _dt
import json
import os
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, Mapping

from harness.budget import BudgetExceeded, CostLedger
from harness.jsonl_store import JsonlStore

Worker = Callable[[Any], Awaitable[Mapping[str, Any]]]
Sleep = Callable[[float], Awaitable[Any]]


def _find(exc: BaseException, types: type | tuple[type, ...]) -> BaseException | None:
    """``exc`` itself, or the first exception of ``types`` inside (nested) exception groups."""
    return _find_if(exc, lambda e: isinstance(e, types))


def _find_if(exc: BaseException, pred: Callable[[BaseException], bool]) -> BaseException | None:
    """``exc`` itself, or the first exception inside (nested) exception groups, for which ``pred`` is true."""
    if pred(exc):
        return exc
    if isinstance(exc, BaseExceptionGroup):
        for sub in exc.exceptions:
            found = _find_if(sub, pred)
            if found is not None:
                return found
    return None


def default_key(task: Any) -> str:
    """A task's key: ``task["key"]`` for mappings, else ``task.key``."""
    if isinstance(task, Mapping):
        return str(task["key"])
    return str(getattr(task, "key"))


@dataclass
class RunStats:
    total: int = 0
    skipped: int = 0
    done: int = 0
    failed: int = 0
    stopped_reason: str | None = None
    started: float = field(default_factory=time.monotonic)
    finish_times: collections.deque = field(default_factory=lambda: collections.deque(maxlen=10_000))
    requeued: int = 0
    network_pauses: int = 0
    paused: str | None = None  # "network" or "stop_file" while new starts are held


class Progress:
    """Formats progress lines: done/total, rate over a trailing window, ETA, spend."""

    def __init__(
        self,
        stats: RunStats,
        ledger: CostLedger | None,
        window_s: float = 300.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.stats = stats
        self.ledger = ledger
        self.window_s = window_s
        self.clock = clock
        self.spent_at_start = ledger.spent_usd if ledger is not None else 0.0

    def rate_per_min(self) -> float:
        now = self.clock()
        recent = [t for t in self.stats.finish_times if now - t <= self.window_s]
        if not recent:
            return 0.0
        span = max(1.0, min(self.window_s, now - self.stats.started))
        return len(recent) / span * 60.0

    def remaining(self) -> int:
        s = self.stats
        return max(0, s.total - s.skipped - s.done - s.failed)

    def eta_s(self) -> float | None:
        rate = self.rate_per_min()
        return self.remaining() / rate * 60.0 if rate > 0 else None

    def projected_usd(self) -> float | None:
        if self.ledger is None:
            return None
        finished = self.stats.done + self.stats.failed
        if finished <= 0:
            return None
        spent = self.ledger.spent_usd
        return spent + (spent - self.spent_at_start) / finished * self.remaining()

    def line(self) -> str:
        s = self.stats
        finished = s.done + s.failed
        eta = self.eta_s()
        parts = [
            f"{s.skipped + finished}/{s.total} done",
            f"{self.rate_per_min():.1f}/min",
            f"ETA {_fmt_duration(eta) if eta is not None else '?'}",
        ]
        if s.failed:
            parts.insert(1, f"{s.failed} failed")
        if s.paused:
            parts.append(f"PAUSED ({s.paused})")
        if self.ledger is not None:
            parts.append(f"spent ${self.ledger.spent_usd:.3f}")
            projected = self.projected_usd()
            if projected is not None:
                parts.append(f"projected ${projected:.2f}")
        return "[run] " + " | ".join(parts)

    def status(self) -> dict[str, Any]:
        s = self.stats
        return {
            "time_utc": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
            "total": s.total,
            "skipped": s.skipped,
            "done": s.done,
            "failed": s.failed,
            "remaining": self.remaining(),
            "rate_per_min": round(self.rate_per_min(), 2),
            "eta_s": None if self.eta_s() is None else round(self.eta_s() or 0.0),
            "spent_usd": None if self.ledger is None else round(self.ledger.spent_usd, 4),
            "projected_usd": None if self.projected_usd() is None else round(self.projected_usd() or 0.0, 2),
            "requeued": s.requeued,
            "network_pauses": s.network_pauses,
            "paused": s.paused,
            "stopped_reason": s.stopped_reason,
            "line": self.line(),
        }


def _fmt_duration(seconds: float) -> str:
    seconds = int(round(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}m"
    if m:
        return f"{m}m{s:02d}s"
    return f"{s}s"


def write_json_atomic(path: str | os.PathLike[str], data: Mapping[str, Any]) -> None:
    """Write ``data`` to ``path`` through a temporary file and ``os.replace`` (readers never see half a file)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(dict(data), f, ensure_ascii=False, indent=1, default=str)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


class NetworkGuard:
    """Sliding window of task outcomes and the ok / down / probing state of the network pause."""

    def __init__(self, window_s: float, fail_ratio: float, min_samples: int) -> None:
        self.window_s = window_s
        self.fail_ratio = fail_ratio
        self.min_samples = max(1, int(min_samples))
        self.events: collections.deque[tuple[float, bool]] = collections.deque()
        self.state = "ok"
        self.down_since: float | None = None
        self.sleeper = False
        self.probe_busy = False
        self.changed = asyncio.Event()

    def _trim(self, now: float) -> None:
        while self.events and now - self.events[0][0] > self.window_s:
            self.events.popleft()

    def record(self, now: float, failed: bool) -> None:
        self.events.append((now, failed))
        self._trim(now)

    def failing(self, now: float) -> bool:
        self._trim(now)
        n = len(self.events)
        if n < self.min_samples:
            return False
        return sum(1 for _, f in self.events if f) / n > self.fail_ratio

    def wake(self) -> None:
        """Wake every task waiting on a state change (they re-check; used on stop)."""
        old, self.changed = self.changed, asyncio.Event()
        old.set()

    def set_state(self, state: str, now: float) -> None:
        if state == self.state:
            return
        if state == "ok":
            self.events.clear()
            self.down_since = None
            self.probe_busy = False
        elif self.down_since is None:
            self.down_since = now
        if state == "probing":
            self.probe_busy = False
        self.state = state
        self.wake()  # every waiter re-checks the new state


async def run_tasks(
    tasks: Iterable[Any],
    worker: Worker,
    store: JsonlStore,
    concurrency: int = 8,
    *,
    key: Callable[[Any], str] = default_key,
    priority: Callable[[Any], Any] | None = None,
    ledger: CostLedger | None = None,
    progress: bool | Callable[[str], None] = True,
    progress_every_s: float = 15.0,
    clock: Callable[[], float] = time.monotonic,
    sleep: Sleep = asyncio.sleep,
    stop_file: str | os.PathLike[str] | None = None,
    stop_file_wait: bool = False,
    stop_file_poll_s: float = 30.0,
    status_file: str | os.PathLike[str] | None = None,
    status_every_s: float = 600.0,
    transient: Callable[[BaseException], bool] | None = None,
    max_requeues: int = 3,
    net_window_s: float = 120.0,
    net_fail_ratio: float = 0.5,
    net_min_samples: int = 4,
    net_pause_s: float = 60.0,
    net_give_up_s: float | None = None,
) -> RunStats:
    """Run ``worker(task)`` for every task not yet in ``store``.

    The worker returns a dict; the runner sets ``key`` to the task key (a
    different ``key`` returned by the worker is moved to ``worker_key``), adds
    ``status="ok"`` (if missing) and appends it to the store.
    ``progress``: True prints to stdout, False is silent, a callable receives
    each progress line. See the module docstring for ``priority``,
    ``stop_file``, ``status_file`` and the network-down pause (``transient``).
    """
    task_list = list(tasks)
    keys = [key(t) for t in task_list]
    if len(set(keys)) != len(keys):
        raise ValueError("task keys must be unique")

    stats = RunStats(total=len(task_list), started=clock())
    done = store.done_keys()
    pending = [(k, t) for k, t in zip(keys, task_list) if k not in done]
    stats.skipped = stats.total - len(pending)
    if priority is not None:
        pending.sort(key=lambda kt: priority(kt[1]))  # stable: ties keep list order

    emit: Callable[[str], None] | None
    if progress is True:
        emit = lambda s: print(s, flush=True)  # noqa: E731
    elif callable(progress):
        emit = progress
    else:
        emit = None
    prog = Progress(stats, ledger, clock=clock)
    last_emit = [clock()]

    def maybe_emit(force: bool = False) -> None:
        if emit is None:
            return
        now = clock()
        if force or now - last_emit[0] >= progress_every_s:
            last_emit[0] = now
            emit(prog.line())

    status_warned = [False]

    def write_status() -> None:
        if status_file is None:
            return
        try:
            write_json_atomic(status_file, prog.status())
        except OSError as exc:  # e.g. a reader holds the file on Windows; try again next time
            if emit is not None and not status_warned[0]:
                emit(f"[run] could not write status file ({exc}); will keep trying")
            status_warned[0] = True

    # Priority queue of (rank, key, task): rank is the start order, unique, so
    # tasks themselves are never compared. A requeued task keeps its rank.
    queue: asyncio.PriorityQueue = asyncio.PriorityQueue()
    for rank, (k, t) in enumerate(pending):
        queue.put_nowait((rank, k, t))
    stop = asyncio.Event()
    stop_path = Path(stop_file) if stop_file is not None else None
    net = NetworkGuard(net_window_s, net_fail_ratio, net_min_samples) if transient is not None else None
    requeues: dict[str, int] = collections.Counter()

    def stop_with(reason: str, message: str) -> None:
        if not stop.is_set():
            stats.stopped_reason = reason
            stop.set()
            if net is not None:
                net.wake()  # tasks waiting for the network must see the stop
            if emit is not None:
                emit(message)

    def budget_stop(exc: BaseException) -> None:
        stop_with("budget", f"[run] budget cap reached, finishing in-flight tasks: {exc}")

    def is_transient(exc: BaseException) -> bool:
        if transient is None:
            return False
        try:
            return _find_if(exc, transient) is not None
        except Exception:  # a buggy predicate must not hide the real error
            return False

    async def gate() -> str | None:
        """Wait until a new task may start. Returns "go", "probe", or None (stop)."""
        while True:
            if stop.is_set():
                return None
            if stop_path is not None and stop_path.exists():
                if not stop_file_wait:
                    stop_with("stop_file", f"[run] {stop_path} exists: no new tasks, finishing in-flight tasks")
                    return None
                if stats.paused != "stop_file":
                    stats.paused = "stop_file"
                    if emit is not None:
                        emit(f"[run] {stop_path} exists: queue paused until it is deleted")
                    write_status()
                await sleep(stop_file_poll_s)
                continue
            if stats.paused == "stop_file":
                stats.paused = None
                if emit is not None:
                    emit("[run] stop file removed: queue resumed")
            if net is None:
                return "go"
            now = clock()
            if net.state == "ok" and net.failing(now):
                net.set_state("down", now)
                stats.network_pauses += 1
                stats.paused = "network"
                if emit is not None:
                    emit(f"[run] most recent tasks failed with network errors: pausing {net_pause_s:.0f}s")
                write_status()
            if net.state == "down":
                if net_give_up_s is not None and net.down_since is not None and now - net.down_since >= net_give_up_s:
                    stop_with("network", f"[run] network down for {now - net.down_since:.0f}s: stopping")
                    return None
                if not net.sleeper:
                    net.sleeper = True
                    try:
                        await sleep(net_pause_s)
                    finally:
                        net.sleeper = False
                    if net.state == "down":
                        net.set_state("probing", clock())
                        if emit is not None:
                            emit("[run] trying one task to test the network")
                else:
                    await net.changed.wait()
                continue
            if net.state == "probing":
                if net.probe_busy:
                    await net.changed.wait()
                    continue
                net.probe_busy = True
                return "probe"
            return "go"

    def network_ok() -> None:
        if net is None:
            return
        now = clock()
        net.record(now, failed=False)
        if net.state != "ok":
            net.set_state("ok", now)
            stats.paused = None
            if emit is not None:
                emit("[run] network back: queue resumed")

    async def run_one(rank: int, k: str, t: Any, probe: bool) -> None:
        try:
            result = await worker(t)
        except BudgetExceeded as exc:
            network_ok()
            budget_stop(exc)
            return
        except BaseExceptionGroup as eg:  # e.g. a worker using asyncio.TaskGroup
            if _find(eg, (KeyboardInterrupt, SystemExit)) is not None:
                raise
            budget = _find(eg, BudgetExceeded)
            if budget is not None:
                network_ok()
                budget_stop(budget)
                return
            if not isinstance(eg, Exception):  # only cancellations etc. inside
                raise
            if is_transient(eg):
                transient_failure(rank, k, t, eg, probe)
                return
            network_ok()
            record_error(k, eg)
            return
        except Exception as exc:  # recorded, retried on resume
            if is_transient(exc):
                transient_failure(rank, k, t, exc, probe)
                return
            network_ok()
            record_error(k, exc)
            return
        network_ok()
        record = dict(result or {})
        # The stored key is always the task key, or the task would never count as
        # done and would run (and be paid) again on every resume. A different key
        # from the worker (e.g. a CallResult's call key) is kept as worker_key.
        if "key" in record and record["key"] != k:
            record["worker_key"] = record["key"]
        record["key"] = k
        record.setdefault("status", "ok")
        store.append(record)
        stats.done += 1
        stats.finish_times.append(clock())
        maybe_emit()

    def transient_failure(rank: int, k: str, t: Any, exc: BaseException, probe: bool) -> None:
        assert net is not None
        now = clock()
        net.record(now, failed=True)
        if probe and net.state == "probing":
            net.set_state("down", now)
            stats.paused = "network"
            if emit is not None:
                emit(f"[run] network still failing: pausing {net_pause_s:.0f}s")
        outage = net.state != "ok" or net.failing(now)
        if not outage:
            requeues[k] += 1
            if requeues[k] > max_requeues:
                record_error(k, exc)
                return
        stats.requeued += 1
        queue.put_nowait((rank, k, t))

    def record_error(k: str, exc: BaseException) -> None:
        stats.failed += 1
        stats.finish_times.append(clock())
        store.append(
            {
                "key": k,
                "status": "error",
                "error": f"{type(exc).__name__}: {exc}",
                "traceback": traceback.format_exc(limit=5),
            }
        )
        maybe_emit()

    async def loop() -> None:
        while True:
            ticket = await gate()
            if ticket is None:
                return
            try:
                rank, k, t = queue.get_nowait()
            except asyncio.QueueEmpty:
                if ticket == "probe" and net is not None:
                    net.set_state("ok", clock())  # nothing left to test: release the waiters
                return
            await run_one(rank, k, t, ticket == "probe")

    async def heartbeat() -> None:  # real time, whatever ``sleep`` is
        while True:
            await asyncio.sleep(status_every_s)
            write_status()

    if emit is not None and stats.skipped:
        emit(f"[run] resuming: {stats.skipped} of {stats.total} tasks already done")
    write_status()
    beat = asyncio.create_task(heartbeat()) if status_file is not None else None
    workers = [asyncio.create_task(loop()) for _ in range(max(1, min(concurrency, len(pending) or 1)))]
    try:
        await asyncio.gather(*workers)
    except BaseException as exc:
        # Ctrl-C / cancellation, or an unexpected error outside a worker's own
        # try (e.g. the disk is full when writing): never leave workers running.
        interrupted = _find(exc, (asyncio.CancelledError, KeyboardInterrupt)) is not None
        stats.stopped_reason = "interrupted" if interrupted else "error"
        stop.set()
        for w in workers:
            w.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
        await _cancel(beat)
        maybe_emit(force=True)
        write_status()
        if emit is not None:
            what = "interrupted" if interrupted else f"stopped by {type(exc).__name__}: {exc}"
            emit(f"[run] {what}; completed results are saved, re-run to resume")
        raise
    await _cancel(beat)
    if stats.stopped_reason is None:
        stats.stopped_reason = "complete" if queue.empty() else "stopped"
    stats.paused = None
    maybe_emit(force=True)
    write_status()
    return stats


async def _cancel(task: asyncio.Task | None) -> None:
    if task is None:
        return
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
