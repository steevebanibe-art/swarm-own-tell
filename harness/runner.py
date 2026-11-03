"""Resumable, bounded-concurrency task runner.

``run_tasks`` takes a list of tasks (each with a unique key), an async worker
and a ``JsonlStore``. Tasks whose key is already in the store are skipped, so
re-running the same command resumes where it stopped. Each finished task is
written immediately (one fsync'd line).

Stopping:
* ``BudgetExceeded`` from any worker: no new task starts, tasks already in
  flight finish and are written, then the run returns with
  ``stopped_reason="budget"``. This also holds when the worker fans out its
  calls with ``asyncio.TaskGroup`` and the BudgetExceeded arrives wrapped in
  an ``ExceptionGroup`` (at any depth).
* KeyboardInterrupt / cancellation: workers are cancelled, the store holds only
  complete lines, a summary is printed and the interruption is re-raised.
  Unfinished tasks run again on the next resume.
* Any other exception in a worker is written as ``{"key", "status": "error",
  "error"}`` (retried on resume, since ``done_keys`` ignores errors).
* An exception outside the worker (e.g. writing the store fails): the other
  workers are cancelled and the exception is re-raised
  (``stopped_reason="error"``).
"""

from __future__ import annotations

import asyncio
import collections
import time
import traceback
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Iterable, Mapping

from harness.budget import BudgetExceeded, CostLedger
from harness.jsonl_store import JsonlStore

Worker = Callable[[Any], Awaitable[Mapping[str, Any]]]


def _find(exc: BaseException, types: type | tuple[type, ...]) -> BaseException | None:
    """``exc`` itself, or the first exception of ``types`` inside (nested) exception groups."""
    if isinstance(exc, types):
        return exc
    if isinstance(exc, BaseExceptionGroup):
        for sub in exc.exceptions:
            found = _find(sub, types)
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

    def line(self) -> str:
        s = self.stats
        todo = s.total - s.skipped
        finished = s.done + s.failed
        remaining = max(0, todo - finished)
        rate = self.rate_per_min()
        eta = _fmt_duration(remaining / rate * 60.0) if rate > 0 else "?"
        parts = [
            f"{s.skipped + finished}/{s.total} done",
            f"{rate:.1f}/min",
            f"ETA {eta}",
        ]
        if s.failed:
            parts.insert(1, f"{s.failed} failed")
        if self.ledger is not None:
            spent = self.ledger.spent_usd
            run_spent = spent - self.spent_at_start
            parts.append(f"spent ${spent:.3f}")
            if finished > 0:
                projected = spent + run_spent / finished * remaining
                parts.append(f"projected ${projected:.2f}")
        return "[run] " + " | ".join(parts)


def _fmt_duration(seconds: float) -> str:
    seconds = int(round(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}m"
    if m:
        return f"{m}m{s:02d}s"
    return f"{s}s"


async def run_tasks(
    tasks: Iterable[Any],
    worker: Worker,
    store: JsonlStore,
    concurrency: int = 8,
    *,
    key: Callable[[Any], str] = default_key,
    ledger: CostLedger | None = None,
    progress: bool | Callable[[str], None] = True,
    progress_every_s: float = 15.0,
    clock: Callable[[], float] = time.monotonic,
) -> RunStats:
    """Run ``worker(task)`` for every task not yet in ``store``.

    The worker returns a dict; the runner adds ``key`` (if missing) and
    ``status="ok"`` (if missing) and appends it to the store.
    ``progress``: True prints to stdout, False is silent, a callable receives
    each progress line.
    """
    task_list = list(tasks)
    keys = [key(t) for t in task_list]
    if len(set(keys)) != len(keys):
        raise ValueError("task keys must be unique")

    stats = RunStats(total=len(task_list), started=clock())
    done = store.done_keys()
    pending = [(k, t) for k, t in zip(keys, task_list) if k not in done]
    stats.skipped = stats.total - len(pending)

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

    queue: asyncio.Queue = asyncio.Queue()
    for item in pending:
        queue.put_nowait(item)
    stop = asyncio.Event()

    def budget_stop(exc: BaseException) -> None:
        if not stop.is_set():
            stats.stopped_reason = "budget"
            stop.set()
            if emit is not None:
                emit(f"[run] budget cap reached, finishing in-flight tasks: {exc}")

    async def run_one(k: str, t: Any) -> None:
        try:
            result = await worker(t)
        except BudgetExceeded as exc:
            budget_stop(exc)
            return
        except BaseExceptionGroup as eg:  # e.g. a worker using asyncio.TaskGroup
            if _find(eg, (KeyboardInterrupt, SystemExit)) is not None:
                raise
            budget = _find(eg, BudgetExceeded)
            if budget is not None:
                budget_stop(budget)
                return
            if not isinstance(eg, Exception):  # only cancellations etc. inside
                raise
            record_error(k, eg)
            return
        except Exception as exc:  # recorded, retried on resume
            record_error(k, exc)
            return
        record = dict(result or {})
        record.setdefault("key", k)
        record.setdefault("status", "ok")
        store.append(record)
        stats.done += 1
        stats.finish_times.append(clock())
        maybe_emit()

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
        while not stop.is_set():
            try:
                k, t = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            await run_one(k, t)

    if emit is not None and stats.skipped:
        emit(f"[run] resuming: {stats.skipped} of {stats.total} tasks already done")
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
        maybe_emit(force=True)
        if emit is not None:
            what = "interrupted" if interrupted else f"stopped by {type(exc).__name__}: {exc}"
            emit(f"[run] {what}; completed results are saved, re-run to resume")
        raise
    if stats.stopped_reason is None:
        stats.stopped_reason = "complete" if queue.empty() else "stopped"
    maybe_emit(force=True)
    return stats
