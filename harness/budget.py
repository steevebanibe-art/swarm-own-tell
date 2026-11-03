"""Hard budget cap backed by an append-only JSONL cost ledger.

Every settled call adds one line ``{ts, key, model, usage, cost_usd, batch}``.
On start-up the ledger replays its file, so the amount already spent survives
restarts and crashes. Before each call the caller reserves the call's worst
case; the reservation is refused (``BudgetExceeded``) when

    spent + outstanding reservations + worst case > hard cap.

Concurrency: all state changes happen under a ``threading.Lock`` that is never
held across an ``await``, so the ledger is safe for asyncio tasks (and threads).
Because every in-flight call holds its worst case, concurrent calls cannot
push the total past the cap unless a call costs more than its own worst case.

One process per ledger file: in-flight reservations live in memory, so two
processes appending to the same file could each spend the whole cap. The
first ``CostLedger`` on a file therefore takes an OS-level lock on
``<ledger>.lock`` (released by ``close()``, garbage collection or process
exit, including a crash); a second *process* gets ``LedgerLocked``. Several
``CostLedger`` objects on the same file inside *one* process share a single
accounting state (spent, reservations, keys), so a re-created object (e.g. a
notebook cell run twice) cannot double the cap either. To run several
processes at once, give each its own ledger file and its own slice of the cap.
"""

from __future__ import annotations

import itertools
import os
import threading
import time
import weakref
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from harness.filelock import os_lock, os_unlock
from harness.jsonl_store import JsonlStore
from harness.pricing import usage_to_dict, worst_case_cost_usd

DEFAULT_HARD_CAP_USD = 22.00


class BudgetExceeded(RuntimeError):
    """Raised when a reservation would take the total past the hard cap."""

    def __init__(self, requested: float, spent: float, outstanding: float, cap: float) -> None:
        self.requested = requested
        self.spent = spent
        self.outstanding = outstanding
        self.cap = cap
        super().__init__(
            f"Budget cap reached: spent ${spent:.4f} + reserved ${outstanding:.4f} "
            f"+ requested ${requested:.4f} > cap ${cap:.2f}"
        )


class LedgerLocked(RuntimeError):
    """Raised when another process already holds the ledger file."""


@dataclass
class Reservation:
    """Money set aside for one call (or one batch) until it is settled or released."""

    id: int
    amount_usd: float
    created: float = field(default_factory=time.time)
    active: bool = True
    tag: str | None = None


# ------------------------------------------------------- one process per file
_os_lock = os_lock  # kept under the old names
_os_unlock = os_unlock


class _SharedState:
    """Accounting state shared by every CostLedger on one file in this process."""

    def __init__(self, path: Path, lock_fd: int) -> None:
        self.lock_fd = lock_fd
        self.refs = 0
        self.store = JsonlStore(path)
        self.lock = threading.Lock()
        self.ids = itertools.count(1)
        self.outstanding: dict[int, Reservation] = {}
        self.tags: dict[str, int] = {}
        self.spent = 0.0
        self.lines = 0
        self.key_cost: dict[str, float] = {}
        for rec in self.store.iter_records():  # replay
            cost = float(rec.get("cost_usd") or 0.0)
            self.spent += cost
            self.lines += 1
            if rec.get("key") is not None:
                k = str(rec["key"])
                self.key_cost[k] = self.key_cost.get(k, 0.0) + cost


_REGISTRY: dict[str, _SharedState] = {}
_REGISTRY_GUARD = threading.Lock()


def _open_shared(path: Path) -> tuple[str, _SharedState]:
    path.parent.mkdir(parents=True, exist_ok=True)
    lid = os.path.normcase(str(path.resolve()))
    with _REGISTRY_GUARD:
        shared = _REGISTRY.get(lid)
        if shared is None:
            lock_path = path.with_name(path.name + ".lock")
            fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)
            try:
                _os_lock(fd)
            except OSError:
                os.close(fd)
                raise LedgerLocked(
                    f"{path} is already in use by another process (lock file {lock_path}). "
                    "Use one process per ledger file: stop the other process, or give each "
                    "process its own ledger file and its own slice of the budget cap."
                ) from None
            try:
                shared = _SharedState(path, fd)
            except BaseException:
                _os_unlock(fd)
                os.close(fd)
                raise
            _REGISTRY[lid] = shared
        shared.refs += 1
        return lid, shared


def _close_shared(lid: str) -> None:
    with _REGISTRY_GUARD:
        shared = _REGISTRY.get(lid)
        if shared is None:
            return
        shared.refs -= 1
        if shared.refs > 0:
            return
        del _REGISTRY[lid]
        try:
            _os_unlock(shared.lock_fd)
        except OSError:
            pass
        os.close(shared.lock_fd)


class CostLedger:
    """Tracks money spent and reserved against a hard cap.

    Raises ``LedgerLocked`` if another process holds the same ledger file.
    Call ``close()`` (or use ``with CostLedger(...) as ledger:``) to hand the
    file over to another process before this one exits.
    """

    def __init__(self, path: str | os.PathLike[str], hard_cap_usd: float = DEFAULT_HARD_CAP_USD) -> None:
        self.hard_cap_usd = float(hard_cap_usd)
        self._lid, self._shared = _open_shared(Path(path))
        self._finalizer = weakref.finalize(self, _close_shared, self._lid)
        self._store = self._shared.store
        self._lock = self._shared.lock
        self.spent_at_start = self.spent_usd

    def close(self) -> None:
        """Release this object's hold on the ledger file (idempotent)."""
        self._finalizer()

    @property
    def closed(self) -> bool:
        return not self._finalizer.alive

    def __enter__(self) -> "CostLedger":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def _check_open(self) -> None:
        if self.closed:
            raise RuntimeError(f"CostLedger on {self.path} is closed")

    # ----------------------------------------------------------------- state
    @property
    def path(self):
        return self._store.path

    @property
    def spent_usd(self) -> float:
        with self._lock:
            return self._shared.spent

    @property
    def outstanding_usd(self) -> float:
        with self._lock:
            return sum(r.amount_usd for r in self._shared.outstanding.values())

    @property
    def remaining_usd(self) -> float:
        with self._lock:
            sh = self._shared
            return self.hard_cap_usd - sh.spent - sum(r.amount_usd for r in sh.outstanding.values())

    @property
    def n_lines(self) -> int:
        with self._lock:
            return self._shared.lines

    def has_key(self, key: str) -> bool:
        """True if a line with this key was already settled (used to avoid double counting)."""
        with self._lock:
            return key in self._shared.key_cost

    def cost_with_prefix(self, prefix: str) -> float:
        """Total cost already settled under every key that starts with ``prefix``."""
        with self._lock:
            return sum(c for k, c in self._shared.key_cost.items() if k.startswith(prefix))

    # ------------------------------------------------------------ operations
    def reserve(self, worst_case_usd: float, *, force: bool = False, tag: str | None = None) -> Reservation:
        """Set money aside, or raise BudgetExceeded without changing anything.

        ``force=True`` never raises: use it only for money that is already
        committed (e.g. a batch that exists server-side), so that it still
        counts against the cap and blocks new spending.
        ``tag``: an outstanding reservation with the same tag is replaced by
        this one, so registering the same commitment twice reserves it once.
        """
        self._check_open()
        amount = max(0.0, float(worst_case_usd))
        sh = self._shared
        with self._lock:
            previous = sh.outstanding.get(sh.tags[tag]) if tag is not None and tag in sh.tags else None
            outstanding = sum(r.amount_usd for r in sh.outstanding.values())
            if previous is not None:
                outstanding -= previous.amount_usd
            if not force and sh.spent + outstanding + amount > self.hard_cap_usd + 1e-12:
                raise BudgetExceeded(amount, sh.spent, outstanding, self.hard_cap_usd)
            if previous is not None:
                self._drop_locked(previous)
            res = Reservation(id=next(sh.ids), amount_usd=amount, tag=tag)
            sh.outstanding[res.id] = res
            if tag is not None:
                sh.tags[tag] = res.id
            return res

    def _drop_locked(self, reservation: Reservation) -> None:
        sh = self._shared
        sh.outstanding.pop(reservation.id, None)
        if reservation.tag is not None and sh.tags.get(reservation.tag) == reservation.id:
            del sh.tags[reservation.tag]
        reservation.active = False

    def release(self, reservation: Reservation | None) -> None:
        """Give a reservation back without recording any cost (call failed, nothing billed)."""
        if reservation is None:
            return
        with self._lock:
            self._drop_locked(reservation)

    def release_tag(self, tag: str) -> None:
        """Release the outstanding reservation that carries ``tag`` (if any)."""
        with self._lock:
            rid = self._shared.tags.get(tag)
            res = self._shared.outstanding.get(rid) if rid is not None else None
            if res is not None:
                self._drop_locked(res)

    def settle(
        self,
        reservation: Reservation | None,
        *,
        key: str,
        model: str,
        usage: Any,
        cost_usd: float,
        batch: bool = False,
        extra: dict[str, Any] | None = None,
    ) -> None:
        """Record the real cost of a call (one JSONL line) and release its reservation.

        ``reservation`` may be None for costs that were reserved as a group
        (e.g. a batch, released separately). Works even after ``close()``: a
        cost that was paid is always written.
        """
        record: dict[str, Any] = {
            "ts": time.time(),
            "key": key,
            "model": model,
            "usage": usage_to_dict(usage),
            "cost_usd": float(cost_usd),
            "batch": bool(batch),
        }
        if extra:
            record.update(extra)
        sh = self._shared
        with self._lock:
            # Write first: if the write fails, nothing in memory changes.
            self._store.append(record)
            sh.spent += float(cost_usd)
            sh.lines += 1
            sh.key_cost[str(key)] = sh.key_cost.get(str(key), 0.0) + float(cost_usd)
            if reservation is not None:
                self._drop_locked(reservation)

    # -------------------------------------------------------------- helpers
    @staticmethod
    def worst_case(
        model: str,
        est_input_tokens: int,
        max_tokens: int,
        batch: bool = False,
        input_multiplier: float = 1.0,
    ) -> float:
        """Worst-case USD for one call: estimated input at its tier + all of max_tokens."""
        return worst_case_cost_usd(model, est_input_tokens, max_tokens, batch, input_multiplier)

    def summary(self) -> str:
        return (
            f"spent ${self.spent_usd:.4f} of ${self.hard_cap_usd:.2f} "
            f"(reserved ${self.outstanding_usd:.4f}, {self.n_lines} ledger lines)"
        )
