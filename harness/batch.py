"""Generic helper for the Message Batches API (50 % cheaper, asynchronous).

Limits used (claude-api skill, batches.md, and the rate-limits page):
up to 100,000 requests or 256 MB per batch; results kept 29 days; most
batches end within 1 hour, at most 24 hours. We chunk well inside those
limits (default 10,000 requests and 128 MB of serialized JSON per batch).

Flow:
    runner = BatchRunner(client, ledger, "state/batches.json")
    await runner.submit([(custom_id, params), ...])   # reserves worst case, creates batches
    await runner.wait()                               # polls until processing_status == "ended"
    results = await runner.collect()                  # {custom_id: BatchItemResult}

The state file (written atomically) records every batch id with its custom
ids, so a crashed process can resume: already-submitted custom ids are not
resubmitted, results already written to the ledger are not charged twice
(ledger key ``batch:<batch_id>:<custom_id>``), and on load each uncollected
batch re-reserves only what is not yet settled (worst case minus the costs
already in the ledger for that batch). That money is already committed
server-side, so it is reserved with ``force=True``: loading never fails on the
cap (a warning is logged if the total is over it), and new spending is blocked.

Ambiguous creates: before ``batches.create`` the chunk is written to the state
file as a *pending* entry and its reservation is kept. If the call fails in a
way that does not prove the batch was refused (connection error, timeout,
5xx, Ctrl-C / cancellation), the entry stays pending. The next ``submit``,
``wait`` or ``collect`` lists the server's batches and keeps as candidates
those not already in this state file, with the same number of requests,
created inside the window [created - clock slack, created + create timeout +
clock slack]. The listing covers the whole API key, so a candidate may belong
to another process; identity is therefore checked by custom ids:

* a candidate that has ended is adopted only if the custom ids of its
  results equal the entry's; otherwise it is rejected for good;
* if exactly one unended candidate remains, it is adopted *unverified*, and
  ``collect`` checks its custom ids before settling anything; on a mismatch it
  is un-adopted (nothing settled, reservation kept) and the entry is pending
  again;
* if several unended candidates remain, nothing is adopted: the entry stays
  pending with its reservation held, a warning is logged, and ``wait`` keeps
  checking until the candidates end;
* if none remains, the entry is dropped and its reservation released (its
  custom ids are submitted again by the next ``submit``).

A 4xx is a clear refusal: the entry is dropped at once. ``create`` is sent
with SDK retries disabled, so the SDK itself cannot create a second copy of a
chunk. Custom ids should be unique across every process that shares the API
key (e.g. prefix them with the run name), so that the identity check is
decisive.

One process per state file: the runner takes an OS lock on
``<state>.lock`` (released by ``close()``, garbage collection or process
exit); a second *process* gets ``BatchStateLocked``. Processes that run at
the same time each need their own state file.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable

import anthropic

from harness.budget import CostLedger, Reservation
from harness.client import cache_write_multiplier, check_params, estimate_input_tokens
from harness.filelock import ProcessFileLock
from harness.pricing import cost_usd, usage_to_dict

MAX_REQUESTS_PER_BATCH = 100_000  # documented hard limit
MAX_BYTES_PER_BATCH = 256 * 1024 * 1024  # documented hard limit
CUSTOM_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
CLOCK_SLACK_S = 600.0  # tolerated skew between the local clock and the server's created_at
CREATE_TIMEOUT_S = 600.0  # timeout of one batches.create call (bounds when the server can stamp it)


class BatchStateLocked(RuntimeError):
    """Raised when another process already uses the batch state file."""


@dataclass
class BatchItemResult:
    custom_id: str
    status: str  # succeeded | errored | canceled | expired
    text: str = ""
    stop_reason: str | None = None
    refusal: bool = False
    refusal_category: str | None = None
    refusal_explanation: str | None = None
    usage: dict[str, Any] | None = None
    cost_usd: float = 0.0
    error_type: str | None = None
    batch_id: str | None = None

    def to_record(self) -> dict[str, Any]:
        return dict(self.__dict__)


class BatchRunner:
    """Submit, poll and collect Message Batches with budget accounting and resume."""

    def __init__(
        self,
        client: anthropic.AsyncAnthropic,
        ledger: CostLedger,
        state_path: str | os.PathLike[str],
        *,
        max_requests_per_batch: int = 10_000,
        max_bytes_per_batch: int = 128 * 1024 * 1024,
        poll_interval_s: float = 60.0,
        create_timeout_s: float = CREATE_TIMEOUT_S,
        clock_slack_s: float = CLOCK_SLACK_S,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        log: Callable[[str], None] | None = print,
    ) -> None:
        if not 0 < max_requests_per_batch <= MAX_REQUESTS_PER_BATCH:
            raise ValueError(f"max_requests_per_batch must be in 1..{MAX_REQUESTS_PER_BATCH}")
        if not 0 < max_bytes_per_batch <= MAX_BYTES_PER_BATCH:
            raise ValueError(f"max_bytes_per_batch must be in 1..{MAX_BYTES_PER_BATCH}")
        self.client = client
        self.ledger = ledger
        self.state_path = Path(state_path)
        self.max_requests = max_requests_per_batch
        self.max_bytes = max_bytes_per_batch
        self.poll_interval_s = poll_interval_s
        self.create_timeout_s = float(create_timeout_s)
        self.clock_slack_s = float(clock_slack_s)
        self._sleep = sleep
        self._log = log
        self._reservations: dict[str, Reservation] = {}
        lock_path = self.state_path.with_name(self.state_path.name + ".lock")
        # One process per state file: each process rewrites the whole file from
        # its own view, so a second one would erase the first one's batches.
        self._file_lock = ProcessFileLock(
            lock_path,
            BatchStateLocked,
            f"{self.state_path} is already in use by another process (lock file {lock_path}). "
            "Use one process per batch state file: stop the other process, or give each "
            "process its own state file (and its own ledger file).",
        )
        try:
            self.state: dict[str, Any] = self._load()
        except BaseException:
            self._file_lock.close()
            raise
        # Re-take the reservations of batches submitted (or maybe submitted) but
        # not yet collected, minus what the ledger already holds for them.
        for b in self.state["batches"]:
            if not b.get("collected"):
                ek = _entry_key(b)
                settled = self.ledger.cost_with_prefix(f"batch:{b['id']}:") if b.get("id") else 0.0
                amount = max(0.0, float(b["reserved_usd"]) - settled)
                self._reservations[ek] = self.ledger.reserve(amount, force=True, tag=f"batch:{ek}")
        if self.ledger.remaining_usd < 0:
            self._emit(
                f"[batch] WARNING: batches already submitted take the total past the cap "
                f"({self.ledger.summary()}); no new spending will be allowed"
            )

    def close(self) -> None:
        """Release this runner's hold on the state file (idempotent)."""
        self._file_lock.close()

    def __enter__(self) -> "BatchRunner":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ----------------------------------------------------------------- state
    def _load(self) -> dict[str, Any]:
        if self.state_path.exists():
            with open(self.state_path, "r", encoding="utf-8") as f:
                return json.load(f)
        return {"batches": []}

    def _save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_name(self.state_path.name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.state, f, ensure_ascii=False, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.state_path)

    def _release_entry(self, ek: str) -> None:
        self.ledger.release(self._reservations.pop(ek, None))
        self.ledger.release_tag(f"batch:{ek}")

    def submitted_ids(self) -> set[str]:
        return {cid for b in self.state["batches"] for cid in b["custom_ids"]}

    def _emit(self, s: str) -> None:
        if self._log is not None:
            self._log(s)

    # ----------------------------------------------------------------- submit
    async def submit(self, requests: Iterable[tuple[str, dict[str, Any]]]) -> list[str]:
        """Create batches for every (custom_id, params) not yet submitted.

        ``params`` are Messages API params (model, max_tokens, messages, ...),
        passed through unchanged. Returns the new batch ids. Raises
        BudgetExceeded before creating a chunk whose worst case does not fit.
        """
        await self._reconcile_pending()
        already = self.submitted_ids()
        todo: list[tuple[str, dict[str, Any], float, int]] = []
        seen: set[str] = set()
        for cid, params in requests:
            if not CUSTOM_ID_RE.match(cid):
                raise ValueError(f"custom_id {cid!r} must match {CUSTOM_ID_RE.pattern}")
            if cid in seen:
                raise ValueError(f"duplicate custom_id {cid!r}")
            seen.add(cid)
            if cid in already:
                continue
            check_params(params)
            est = estimate_input_tokens(params)
            worst = self.ledger.worst_case(
                params["model"], est, int(params["max_tokens"]), batch=True,
                input_multiplier=cache_write_multiplier(params),
            )
            size = len(json.dumps({"custom_id": cid, "params": params}, ensure_ascii=False, default=str).encode("utf-8"))
            todo.append((cid, params, worst, size))

        new_ids: list[str] = []
        chunk: list[tuple[str, dict[str, Any], float, int]] = []
        chunk_bytes = 0
        for item in todo:
            if chunk and (len(chunk) >= self.max_requests or chunk_bytes + item[3] > self.max_bytes):
                new_ids.append(await self._create(chunk))
                chunk, chunk_bytes = [], 0
            chunk.append(item)
            chunk_bytes += item[3]
        if chunk:
            new_ids.append(await self._create(chunk))
        return new_ids

    async def _create(self, chunk: list[tuple[str, dict[str, Any], float, int]]) -> str:
        worst = sum(w for _, _, w, _ in chunk)
        pending_id = "pending_" + uuid.uuid4().hex
        reservation = self.ledger.reserve(worst, tag=f"batch:{pending_id}")  # BudgetExceeded before sending
        entry: dict[str, Any] = {
            "id": None,
            "pending_id": pending_id,
            "created": time.time(),
            "custom_ids": [cid for cid, _, _, _ in chunk],
            "models": {cid: params["model"] for cid, params, _, _ in chunk},
            "reserved_usd": worst,
            "status": "pending",
            "collected": False,
        }
        self._reservations[pending_id] = reservation
        self.state["batches"].append(entry)
        try:
            self._save()  # written before sending: a lost response cannot hide a live batch
        except BaseException:
            self.state["batches"].remove(entry)
            self._release_entry(pending_id)
            raise
        client = self.client
        if hasattr(client, "with_options"):
            # No SDK retry (it could create a second copy), and a bounded timeout
            # (it bounds the server time stamp that _reconcile_pending looks for).
            client = client.with_options(max_retries=0, timeout=self.create_timeout_s)
        try:
            batch = await client.messages.batches.create(
                requests=[{"custom_id": cid, "params": params} for cid, params, _, _ in chunk]
            )
        except anthropic.APIStatusError as exc:
            if 400 <= exc.status_code < 500:  # clearly refused: nothing was created
                self._drop_pending(entry)
            raise
        # Any other failure (connection, timeout, cancellation) leaves the entry
        # pending; _reconcile_pending() resolves it on the next submit/wait/collect.
        self._adopt(entry, batch.id, getattr(batch, "processing_status", None))
        self._save()
        self._emit(f"[batch] created {batch.id} with {len(chunk)} requests (worst case ${worst:.4f})")
        return batch.id

    def _adopt(self, entry: dict[str, Any], batch_id: str, status: str | None, *, verified: bool = True) -> None:
        pending_id = entry.pop("pending_id")
        entry["id"] = batch_id
        entry["status"] = status
        if verified:
            entry.pop("verified", None)
        else:
            entry["verified"] = False  # collect() checks its custom ids before settling
        old = self._reservations.pop(pending_id, None)
        amount = old.amount_usd if old is not None else float(entry["reserved_usd"])
        self._reservations[batch_id] = self.ledger.reserve(amount, force=True, tag=f"batch:{batch_id}")
        self.ledger.release(old)
        self.ledger.release_tag(f"batch:{pending_id}")

    def _unadopt(self, entry: dict[str, Any]) -> None:
        """Turn a wrongly adopted entry back into a pending one; its reservation is kept."""
        batch_id = entry["id"]
        pending_id = "pending_" + uuid.uuid4().hex
        entry["rejected_ids"] = sorted(set(entry.get("rejected_ids", [])) | {batch_id})
        entry["id"] = None
        entry["pending_id"] = pending_id
        entry["status"] = "pending"
        entry.pop("verified", None)
        entry.pop("request_counts", None)
        old = self._reservations.pop(batch_id, None)
        amount = old.amount_usd if old is not None else float(entry["reserved_usd"])
        self._reservations[pending_id] = self.ledger.reserve(amount, force=True, tag=f"batch:{pending_id}")
        self.ledger.release(old)
        self.ledger.release_tag(f"batch:{batch_id}")
        self._save()

    def _drop_pending(self, entry: dict[str, Any]) -> None:
        self.state["batches"] = [b for b in self.state["batches"] if b is not entry]
        self._save()
        self._release_entry(entry["pending_id"])

    async def _result_ids(self, batch_id: str) -> set[str]:
        stream = await _maybe_await(self.client.messages.batches.results(batch_id))
        return {item.custom_id async for item in stream}

    async def _reconcile_pending(self) -> None:
        """Adopt or drop entries whose create call ended without a clear answer."""
        pend = sorted((b for b in self.state["batches"] if b.get("id") is None), key=lambda b: b["created"])
        if not pend:
            return
        known = {b["id"] for b in self.state["batches"] if b.get("id")}
        oldest = pend[0]["created"] - self.clock_slack_s
        listed = []
        async for info in self.client.messages.batches.list(limit=100):  # newest first
            created = _ts(info.created_at)
            if created < oldest:
                break
            if info.id not in known:
                listed.append((created, info))
        for b in pend:
            await self._resolve_pending(b, listed, known)

    async def _resolve_pending(self, b: dict[str, Any], listed: list, known: set[str]) -> None:
        n = len(b["custom_ids"])
        lo = b["created"] - self.clock_slack_s
        hi = b["created"] + self.create_timeout_s + self.clock_slack_s
        rejected = set(b.get("rejected_ids", []))
        cands = [
            (c, info) for c, info in listed
            if lo <= c <= hi and _total(info) == n and info.id not in known and info.id not in rejected
        ]
        cands.sort(key=lambda ci: abs(ci[0] - b["created"]))  # closest to our create first
        want = set(b["custom_ids"])
        still_open = []
        for _, info in cands:
            if info.processing_status == "ended":
                if await self._result_ids(info.id) == want:  # identity proven by its custom ids
                    self._adopt(b, info.id, "ended")
                    known.add(info.id)
                    self._save()
                    self._emit(f"[batch] adopted {info.id} for a create whose response was lost ({n} requests, custom ids checked)")
                    return
                rejected.add(info.id)  # someone else's batch
            else:
                still_open.append(info)
        if rejected:
            b["rejected_ids"] = sorted(rejected)
        if not still_open:
            self._drop_pending(b)
            self._emit(f"[batch] create of {n} requests did not reach the server; they will be resubmitted")
            return
        if len(still_open) == 1:
            info = still_open[0]
            self._adopt(b, info.id, info.processing_status, verified=False)
            known.add(info.id)
            self._save()
            self._emit(
                f"[batch] adopted {info.id} for a create whose response was lost ({n} requests); "
                "its custom ids are checked before any result is settled"
            )
            return
        self._save()
        self._emit(
            f"[batch] WARNING: {len(still_open)} server batches ({', '.join(i.id for i in still_open)}) could be "
            f"the lost create of {n} requests; none adopted, reservation kept; resolved by custom ids once they end"
        )

    # ----------------------------------------------------------------- poll
    async def wait(self, timeout_s: float | None = None) -> None:
        """Poll every pending batch until its processing_status is "ended".

        Also keeps resolving lost creates that are still ambiguous (several
        candidate batches on the server) until they are adopted or dropped.
        """
        start = time.monotonic()
        while True:
            await self._reconcile_pending()
            pending = [b for b in self.state["batches"] if b.get("id") and b.get("status") != "ended"]
            for b in pending:
                info = await self.client.messages.batches.retrieve(b["id"])
                b["status"] = info.processing_status
                counts = getattr(info, "request_counts", None)
                if counts is not None:
                    b["request_counts"] = {
                        k: getattr(counts, k, None)
                        for k in ("processing", "succeeded", "errored", "canceled", "expired")
                    }
            if pending:
                self._save()
            still = [b for b in self.state["batches"] if not b.get("id") or b.get("status") != "ended"]
            if not still:
                return
            done = sum(1 for b in self.state["batches"] if b.get("status") == "ended")
            self._emit(f"[batch] {done}/{len(self.state['batches'])} batches ended; next poll in {self.poll_interval_s:.0f}s")
            if timeout_s is not None and time.monotonic() - start > timeout_s:
                raise TimeoutError(f"{len(still)} batch(es) still processing after {timeout_s}s")
            await self._sleep(self.poll_interval_s)

    # ----------------------------------------------------------------- collect
    async def collect(self) -> dict[str, BatchItemResult]:
        """Fetch results of ended batches (any order), settle costs with batch=True."""
        await self._reconcile_pending()
        out: dict[str, BatchItemResult] = {}
        unadopted = False
        for b in list(self.state["batches"]):
            if not b.get("id") or b.get("status") != "ended":
                continue
            stream = await _maybe_await(self.client.messages.batches.results(b["id"]))
            if b.get("verified") is False:
                # Adopted after a lost create without proof that it is ours:
                # check its custom ids before settling anything.
                items = [item async for item in stream]
                if {item.custom_id for item in items} != set(b["custom_ids"]):
                    bid = b["id"]
                    self._unadopt(b)
                    unadopted = True
                    self._emit(f"[batch] WARNING: {bid} is not ours (custom ids differ); un-adopted, nothing settled")
                    continue
                b.pop("verified", None)
                self._save()
                stream = _aiter(items)
            async for item in stream:
                r = self._item(b["id"], item)
                out[r.custom_id] = r
            if not b.get("collected"):
                b["collected"] = True
                self._save()
                self._release_entry(b["id"])
        if unadopted:
            await self._reconcile_pending()  # adopt the right batch, or drop so the next submit resends
        return out

    def _item(self, batch_id: str, item: Any) -> BatchItemResult:
        cid = item.custom_id
        res = item.result
        kind = res.type
        if kind != "succeeded":
            err = getattr(res, "error", None)
            etype = getattr(getattr(err, "error", err), "type", None) if err is not None else None
            return BatchItemResult(cid, kind, error_type=etype, batch_id=batch_id)
        msg = res.message
        usage = usage_to_dict(msg.usage)
        # Price by the model id we sent (the response may echo a dated id).
        params_model = self._sent_model(batch_id, cid) or getattr(msg, "model", None) or "unknown"
        cost = cost_usd(params_model, usage, batch=True)
        ledger_key = f"batch:{batch_id}:{cid}"
        if not self.ledger.has_key(ledger_key):
            self.ledger.settle(None, key=ledger_key, model=params_model, usage=usage, cost_usd=cost, batch=True)
        text = "".join(b.text for b in msg.content if getattr(b, "type", None) == "text")
        details = getattr(msg, "stop_details", None)
        return BatchItemResult(
            custom_id=cid,
            status="succeeded",
            text=text,
            stop_reason=msg.stop_reason,
            refusal=msg.stop_reason == "refusal",
            refusal_category=getattr(details, "category", None) if details is not None else None,
            refusal_explanation=getattr(details, "explanation", None) if details is not None else None,
            usage=usage,
            cost_usd=cost,
            batch_id=batch_id,
        )

    def _sent_model(self, batch_id: str, custom_id: str) -> str | None:
        for b in self.state["batches"]:
            if b["id"] == batch_id:
                return b.get("models", {}).get(custom_id)
        return None


def _entry_key(b: dict[str, Any]) -> str:
    return b.get("id") or b["pending_id"]


def _ts(value: Any) -> float:
    if hasattr(value, "timestamp"):
        return float(value.timestamp())
    return float(value)


def _total(info: Any) -> int:
    counts = getattr(info, "request_counts", None)
    if counts is None:
        return -1
    return sum(int(getattr(counts, k, 0) or 0) for k in ("processing", "succeeded", "errored", "canceled", "expired"))


async def _aiter(items: list[Any]):
    for item in items:
        yield item


async def _maybe_await(value: Any) -> Any:
    if asyncio.iscoroutine(value) or isinstance(value, asyncio.Future):
        return await value
    return value
