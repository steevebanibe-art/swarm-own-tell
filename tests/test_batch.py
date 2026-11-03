import json

import anthropic
import httpx2
import pytest

from harness.batch import BatchRunner
from harness.budget import BudgetExceeded, CostLedger
from harness.pricing import cost_usd
from tests.conftest import message_body, read_lines

M = "claude-haiku-5-5"
USAGE = {"input_tokens": 1000, "output_tokens": 100, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}


class FakeBatches:
    """Fake /v1/messages/batches endpoints."""

    def __init__(self, polls_before_end: int = 1) -> None:
        self.batches: dict[str, dict] = {}
        self.polls: dict[str, int] = {}
        self.polls_before_end = polls_before_end
        self.created: list[list[str]] = []

    def obj(self, bid: str) -> dict:
        ended = self.polls.get(bid, 0) > self.polls_before_end
        n = len(self.batches[bid]["requests"])
        return {
            "id": bid,
            "type": "message_batch",
            "processing_status": "ended" if ended else "in_progress",
            "request_counts": {"processing": 0 if ended else n, "succeeded": n if ended else 0,
                               "errored": 0, "canceled": 0, "expired": 0},
            "created_at": "2026-11-06T10:00:00Z",
            "expires_at": "2026-11-07T10:00:00Z",
            "ended_at": "2026-11-06T10:30:00Z" if ended else None,
            "cancel_initiated_at": None,
            "archived_at": None,
            "results_url": f"https://api.anthropic.com/v1/messages/batches/{bid}/results" if ended else None,
        }

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        path = request.url.path
        if request.method == "POST" and path == "/v1/messages/batches":
            body = json.loads(request.content)
            bid = f"msgbatch_{len(self.batches) + 1}"
            self.batches[bid] = body
            self.created.append([r["custom_id"] for r in body["requests"]])
            return httpx2.Response(200, json=self.obj(bid))
        if request.method == "GET" and path.endswith("/results"):
            bid = path.split("/")[-2]
            lines = []
            for r in reversed(self.batches[bid]["requests"]):  # results in any order
                cid = r["custom_id"]
                if cid.startswith("ref"):
                    res = {"type": "succeeded", "message": message_body(
                        [], stop_reason="refusal", usage=USAGE,
                        stop_details={"type": "refusal", "category": "cyber", "explanation": "declined"})}
                elif cid.startswith("bad"):
                    res = {"type": "errored", "error": {"type": "error", "error": {"type": "invalid_request_error", "message": "x"}}}
                else:
                    res = {"type": "succeeded", "message": message_body([{"type": "text", "text": f"answer {cid}"}], usage=USAGE)}
                lines.append(json.dumps({"custom_id": cid, "result": res}))
            return httpx2.Response(200, content=("\n".join(lines) + "\n").encode(), headers={"content-type": "application/binary"})
        if request.method == "GET":
            bid = path.rsplit("/", 1)[-1]
            self.polls[bid] = self.polls.get(bid, 0) + 1
            return httpx2.Response(200, json=self.obj(bid))
        raise AssertionError(f"unexpected {request.method} {path}")


def params(text: str) -> dict:
    return {"model": M, "max_tokens": 200, "output_config": {"effort": "low"},
            "messages": [{"role": "user", "content": text}]}


async def test_batch_submit_poll_collect_and_resume(tmp_path, mock_api, fake_clock):
    fake = FakeBatches()
    mock_api.routes[("POST", "/v1/messages/batches")] = fake.handle
    mock_api.routes[("GET", "/v1/messages/batches")] = fake.handle
    ledger = CostLedger(tmp_path / "ledger.jsonl", hard_cap_usd=5.0)
    state = tmp_path / "state" / "batches.json"
    br = BatchRunner(mock_api.client(), ledger, state, max_requests_per_batch=2,
                     poll_interval_s=30, sleep=fake_clock.sleep, log=None)

    reqs = [("a", params("one")), ("b", params("two")), ("bad1", params("three"))]
    ids = await br.submit(reqs)
    assert len(ids) == 2 and fake.created == [["a", "b"], ["bad1"]]  # chunked
    assert ledger.outstanding_usd > 0
    sent = fake.batches["msgbatch_1"]["requests"][0]["params"]
    assert sent["output_config"] == {"effort": "low"} and "temperature" not in sent

    # a restarted process does not resubmit and re-takes the reservations
    br2 = BatchRunner(mock_api.client(), ledger, state, max_requests_per_batch=2,
                      poll_interval_s=30, sleep=fake_clock.sleep, log=None)
    assert await br2.submit(reqs) == []
    assert len(fake.batches) == 2

    await br2.wait()
    assert 30 in fake_clock.sleeps
    results = await br2.collect()
    assert set(results) == {"a", "b", "bad1"}
    assert results["a"].status == "succeeded" and results["a"].text == "answer a"
    assert results["bad1"].status == "errored" and results["bad1"].error_type == "invalid_request_error"
    expected = cost_usd(M, USAGE, batch=True)
    assert results["a"].cost_usd == pytest.approx(expected)
    lines = read_lines(tmp_path / "ledger.jsonl")
    assert len(lines) == 2 and all(l["batch"] for l in lines)
    assert ledger.spent_usd == pytest.approx(2 * expected)

    # collecting again does not charge twice
    await br2.collect()
    assert len(read_lines(tmp_path / "ledger.jsonl")) == 2
    assert json.loads(state.read_text(encoding="utf-8"))["batches"][0]["collected"] is True


async def test_batch_budget_blocks_before_submit(tmp_path, mock_api, fake_clock):
    fake = FakeBatches()
    mock_api.routes[("POST", "/v1/messages/batches")] = fake.handle
    ledger = CostLedger(tmp_path / "ledger.jsonl", hard_cap_usd=0.00001)
    br = BatchRunner(mock_api.client(), ledger, tmp_path / "s.json", sleep=fake_clock.sleep, log=None)
    with pytest.raises(BudgetExceeded):
        await br.submit([("a", params("x"))])
    assert mock_api.requests == []


async def test_batch_rejects_bad_input(tmp_path, mock_api):
    ledger = CostLedger(tmp_path / "ledger.jsonl")
    br = BatchRunner(mock_api.client(), ledger, tmp_path / "s.json", log=None)
    with pytest.raises(ValueError):
        await br.submit([("has space", params("x"))])
    with pytest.raises(ValueError):
        await br.submit([("a", params("x")), ("a", params("y"))])
    with pytest.raises(ValueError):
        await br.submit([("a", {**params("x"), "temperature": 0})])
    assert mock_api.requests == []


async def test_batch_refusal_is_reported_and_billed(tmp_path, mock_api, fake_clock):
    fake = FakeBatches(polls_before_end=0)
    mock_api.routes[("POST", "/v1/messages/batches")] = fake.handle
    mock_api.routes[("GET", "/v1/messages/batches")] = fake.handle
    ledger = CostLedger(tmp_path / "ledger.jsonl", hard_cap_usd=5.0)
    br = BatchRunner(mock_api.client(), ledger, tmp_path / "s.json", sleep=fake_clock.sleep, log=None)
    await br.submit([("ref1", params("x")), ("ok1", params("y"))])
    await br.wait()
    res = await br.collect()
    r = res["ref1"]
    assert r.refusal and r.stop_reason == "refusal" and r.text == ""
    assert r.refusal_category == "cyber" and r.refusal_explanation == "declined"
    assert res["ok1"].text == "answer ok1"
    assert ledger.n_lines == 2 and ledger.outstanding_usd == 0


# ---------------------------------------------------------------- resume after partial collect
def _write_state(path, batches):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"batches": batches}), encoding="utf-8")


def _entry(bid, reserved, cids=("a", "b")):
    return {"id": bid, "created": 0, "custom_ids": list(cids), "models": {c: M for c in cids},
            "reserved_usd": reserved, "status": "ended", "collected": False}


def test_reload_reserves_only_the_unsettled_part(tmp_path, mock_api):
    ledger = CostLedger(tmp_path / "ledger.jsonl", hard_cap_usd=1.0)
    # a previous collect() settled item "a" and died before marking the batch collected
    ledger.settle(None, key="batch:msgbatch_1:a", model=M, usage=None, cost_usd=0.25, batch=True)
    state = tmp_path / "s.json"
    _write_state(state, [_entry("msgbatch_1", 0.8)])
    br = BatchRunner(mock_api.client(), ledger, state, log=None)  # used to raise BudgetExceeded
    assert ledger.outstanding_usd == pytest.approx(0.55)
    assert ledger.spent_usd + ledger.outstanding_usd == pytest.approx(0.8)
    # building a second runner in the same process does not reserve the batch twice
    BatchRunner(mock_api.client(), ledger, state, log=None)
    assert ledger.outstanding_usd == pytest.approx(0.55)
    assert br.state["batches"][0]["id"] == "msgbatch_1"


def test_reload_over_cap_warns_but_loads(tmp_path, mock_api):
    ledger = CostLedger(tmp_path / "ledger.jsonl", hard_cap_usd=1.0)
    state = tmp_path / "s.json"
    _write_state(state, [_entry("msgbatch_1", 0.7), _entry("msgbatch_2", 0.7, ("c",))])
    logs = []
    BatchRunner(mock_api.client(), ledger, state, log=logs.append)  # money already committed
    assert ledger.outstanding_usd == pytest.approx(1.4)
    assert any("WARNING" in l for l in logs)
    with pytest.raises(BudgetExceeded):
        ledger.reserve(0.01)  # but no new spending


# ---------------------------------------------------------------- ambiguous create
class ListingFakeBatches(FakeBatches):
    """Adds GET /v1/messages/batches (list, newest first) and failure modes on create."""

    def __init__(self, fail: str | None = None) -> None:
        super().__init__(polls_before_end=0)
        self.fail = fail  # "lost_response" | "not_received" | "bad_request" | None
        self.created_ts: dict[str, float] = {}
        self.list_calls = 0

    def obj(self, bid: str) -> dict:
        import datetime as dt

        o = super().obj(bid)
        o["created_at"] = dt.datetime.fromtimestamp(self.created_ts[bid], dt.timezone.utc).isoformat()
        return o

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        import time as _time

        path = request.url.path
        if request.method == "POST" and path == "/v1/messages/batches":
            fail, self.fail = self.fail, None
            if fail == "not_received":
                raise httpx2.ConnectError("reset", request=request)
            if fail == "bad_request":
                return httpx2.Response(400, json={"type": "error", "error": {"type": "invalid_request_error", "message": "x"}})
            body = json.loads(request.content)
            bid = f"msgbatch_{len(self.batches) + 1}"
            self.batches[bid] = body
            self.created_ts[bid] = _time.time()
            self.created.append([r["custom_id"] for r in body["requests"]])
            if fail == "lost_response":
                raise httpx2.ReadError("connection dropped", request=request)
            return httpx2.Response(200, json=self.obj(bid))
        if request.method == "GET" and path == "/v1/messages/batches":
            self.list_calls += 1
            data = [self.obj(b) for b in sorted(self.batches, key=lambda b: -self.created_ts[b])]
            return httpx2.Response(200, json={"data": data, "has_more": False,
                                              "first_id": data[0]["id"] if data else None,
                                              "last_id": data[-1]["id"] if data else None})
        return super().handle(request)


def _routes(mock_api, fake):
    mock_api.routes[("POST", "/v1/messages/batches")] = fake.handle
    mock_api.routes[("GET", "/v1/messages/batches")] = fake.handle


async def test_lost_create_response_is_adopted_not_resubmitted(tmp_path, mock_api, fake_clock):
    fake = ListingFakeBatches(fail="lost_response")
    _routes(mock_api, fake)
    ledger = CostLedger(tmp_path / "ledger.jsonl", hard_cap_usd=5.0)
    state = tmp_path / "s.json"
    reqs = [("a", params("one")), ("b", params("two"))]
    br = BatchRunner(mock_api.client(), ledger, state, sleep=fake_clock.sleep, log=None)
    with pytest.raises(anthropic.APIConnectionError):
        await br.submit(reqs)
    saved = json.loads(state.read_text(encoding="utf-8"))["batches"]
    assert len(saved) == 1 and saved[0]["id"] is None and saved[0]["status"] == "pending"
    held = ledger.outstanding_usd
    assert held > 0  # the money stays reserved: the batch may exist

    # restart: the pending entry is matched to the batch the server did create
    br2 = BatchRunner(mock_api.client(), ledger, state, sleep=fake_clock.sleep, log=None)
    assert ledger.outstanding_usd == pytest.approx(held)
    assert await br2.submit(reqs) == []
    assert len(fake.created) == 1  # not billed twice
    assert br2.state["batches"][0]["id"] == "msgbatch_1"
    await br2.wait()
    res = await br2.collect()
    assert set(res) == {"a", "b"}
    assert ledger.n_lines == 2 and ledger.outstanding_usd == 0


async def test_create_that_never_reached_server_is_resubmitted(tmp_path, mock_api, fake_clock):
    fake = ListingFakeBatches(fail="not_received")
    _routes(mock_api, fake)
    ledger = CostLedger(tmp_path / "ledger.jsonl", hard_cap_usd=5.0)
    state = tmp_path / "s.json"
    br = BatchRunner(mock_api.client(), ledger, state, sleep=fake_clock.sleep, log=None)
    with pytest.raises(anthropic.APIConnectionError):
        await br.submit([("a", params("one"))])
    held = ledger.outstanding_usd
    assert held > 0 and fake.created == []
    ids = await br.submit([("a", params("one"))])  # listed: absent, so dropped and resubmitted
    assert ids == ["msgbatch_1"] and fake.created == [["a"]]
    assert ledger.outstanding_usd == pytest.approx(held)  # one reservation, not two
    assert [b["id"] for b in json.loads(state.read_text(encoding="utf-8"))["batches"]] == ["msgbatch_1"]


async def test_create_4xx_releases_at_once(tmp_path, mock_api, fake_clock):
    fake = ListingFakeBatches(fail="bad_request")
    _routes(mock_api, fake)
    ledger = CostLedger(tmp_path / "ledger.jsonl", hard_cap_usd=5.0)
    state = tmp_path / "s.json"
    br = BatchRunner(mock_api.client(), ledger, state, sleep=fake_clock.sleep, log=None)
    with pytest.raises(anthropic.BadRequestError):
        await br.submit([("a", params("one"))])
    assert ledger.outstanding_usd == 0
    assert json.loads(state.read_text(encoding="utf-8"))["batches"] == []


async def test_cancel_during_create_keeps_pending_entry(tmp_path, fake_clock):
    import asyncio

    import anthropic as _anthropic

    received = asyncio.Event()

    async def handler(request):
        received.set()
        await asyncio.Event().wait()

    http = _anthropic.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(handler))
    api = _anthropic.AsyncAnthropic(api_key="test-key", http_client=http, max_retries=0)
    ledger = CostLedger(tmp_path / "ledger.jsonl", hard_cap_usd=5.0)
    state = tmp_path / "s.json"
    br = BatchRunner(api, ledger, state, sleep=fake_clock.sleep, log=None)
    task = asyncio.create_task(br.submit([("a", params("one"))]))
    await asyncio.wait_for(received.wait(), 5)
    task.cancel()  # Ctrl-C while the upload is in flight
    with pytest.raises(asyncio.CancelledError):
        await task
    saved = json.loads(state.read_text(encoding="utf-8"))["batches"]
    assert len(saved) == 1 and saved[0]["id"] is None and saved[0]["custom_ids"] == ["a"]
    assert ledger.outstanding_usd > 0


# ---------------------------------------------------------------- lost create, foreign batches on the same key
class AgingFakeBatches(ListingFakeBatches):
    """Every list call ages each batch by one poll (so batches end over time),
    and batches of other processes can be planted on the server."""

    def plant(self, bid: str, cids, created_ts: float, ended: bool = False) -> None:
        self.batches[bid] = {"requests": [{"custom_id": c, "params": params("z")} for c in cids]}
        self.created_ts[bid] = created_ts
        if ended:
            self.polls[bid] = self.polls_before_end + 1

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        if request.method == "GET" and request.url.path == "/v1/messages/batches":
            for b in self.batches:
                self.polls[b] = self.polls.get(b, 0) + 1
        return super().handle(request)


async def test_lost_create_with_foreign_batch_is_not_misadopted(tmp_path, mock_api, fake_clock):
    import time as _time

    fake = AgingFakeBatches(fail="lost_response")
    fake.polls_before_end = 2  # both batches still processing at the first look
    fake.plant("msgbatch_FOREIGN", ["x0", "x1"], _time.time() - 120)  # another process, same size
    _routes(mock_api, fake)
    ledger = CostLedger(tmp_path / "ledger.jsonl", hard_cap_usd=5.0)
    logs = []
    br = BatchRunner(mock_api.client(), ledger, tmp_path / "s.json", sleep=fake_clock.sleep, log=logs.append)
    reqs = [("a", params("one")), ("b", params("two"))]
    with pytest.raises(anthropic.APIConnectionError):
        await br.submit(reqs)
    mine = next(b for b in fake.batches if b != "msgbatch_FOREIGN")
    await br.wait()  # ambiguous at first; resolved by custom ids once both batches end
    assert any("WARNING" in l and "could be" in l for l in logs)
    assert [b["id"] for b in br.state["batches"]] == [mine]
    res = await br.collect()
    assert set(res) == {"a", "b"}
    keys = [l["key"] for l in read_lines(tmp_path / "ledger.jsonl")]
    assert sorted(keys) == [f"batch:{mine}:a", f"batch:{mine}:b"]  # nothing of the other process settled
    assert ledger.outstanding_usd == 0
    assert len(fake.created) == 1  # not resubmitted


async def test_wrong_unverified_adoption_is_undone_before_settling(tmp_path, mock_api, fake_clock):
    import time as _time

    fake = ListingFakeBatches(fail="not_received")  # our create never reached the server
    fake.batches["msgbatch_FOREIGN"] = {"requests": [{"custom_id": "x0", "params": params("z")}]}
    fake.created_ts["msgbatch_FOREIGN"] = _time.time() - 120  # still processing when we look
    _routes(mock_api, fake)
    ledger = CostLedger(tmp_path / "ledger.jsonl", hard_cap_usd=5.0)
    logs = []
    br = BatchRunner(mock_api.client(), ledger, tmp_path / "s.json", sleep=fake_clock.sleep, log=logs.append)
    with pytest.raises(anthropic.APIConnectionError):
        await br.submit([("a", params("one"))])
    held = ledger.outstanding_usd
    await br.wait()  # the single candidate is adopted unverified, then ends
    assert br.state["batches"][0]["id"] == "msgbatch_FOREIGN" and br.state["batches"][0]["verified"] is False
    assert ledger.outstanding_usd == pytest.approx(held)
    res = await br.collect()
    assert res == {}  # the other process's results are neither returned nor settled
    assert ledger.n_lines == 0
    assert any("is not ours" in l for l in logs)
    assert br.state["batches"] == [] and ledger.outstanding_usd == 0  # dropped: nothing of ours exists
    assert await br.submit([("a", params("one"))]) != []  # so it is sent again
    assert fake.created == [["a"]]


async def test_candidate_created_long_after_the_lost_create_is_ignored(tmp_path, mock_api, fake_clock):
    import time as _time

    now = _time.time()
    fake = ListingFakeBatches()
    fake.batches["msgbatch_LATER"] = {"requests": [{"custom_id": "x0", "params": params("z")}]}
    fake.created_ts["msgbatch_LATER"] = now - 60  # made by someone else long after our create
    _routes(mock_api, fake)
    state = tmp_path / "s.json"
    _write_state(state, [{"id": None, "pending_id": "pending_old", "created": now - 7200, "custom_ids": ["a"],
                          "models": {"a": M}, "reserved_usd": 0.1, "status": "pending", "collected": False}])
    ledger = CostLedger(tmp_path / "ledger.jsonl", hard_cap_usd=5.0)
    br = BatchRunner(mock_api.client(), ledger, state, sleep=fake_clock.sleep, log=None)
    assert ledger.outstanding_usd == pytest.approx(0.1)
    await br.wait()
    assert br.state["batches"] == [] and ledger.outstanding_usd == 0  # dropped, not adopted


# ---------------------------------------------------------------- one process per state file
import subprocess  # noqa: E402
import sys  # noqa: E402
from pathlib import Path  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
_STATE_CHILD = (
    "import sys; sys.path.insert(0, sys.argv[1])\n"
    "import anthropic\n"
    "from harness.batch import BatchRunner, BatchStateLocked\n"
    "from harness.budget import CostLedger\n"
    "led = CostLedger(sys.argv[3], hard_cap_usd=1.0)\n"
    "try:\n"
    "    BatchRunner(anthropic.AsyncAnthropic(api_key='test-key'), led, sys.argv[2], log=None)\n"
    "except BatchStateLocked:\n"
    "    sys.exit(3)\n"
)


def _state_child(state, ledger_path) -> int:
    return subprocess.run([sys.executable, "-c", _STATE_CHILD, str(ROOT), str(state), str(ledger_path)],
                          timeout=25).returncode


def test_second_process_on_same_state_file_is_refused(tmp_path, mock_api):
    state = tmp_path / "state" / "batches.json"
    ledger = CostLedger(tmp_path / "ledger_n5.jsonl", hard_cap_usd=1.0)
    br = BatchRunner(mock_api.client(), ledger, state, log=None)
    br2 = BatchRunner(mock_api.client(), ledger, state, log=None)  # same process: allowed
    br.close()
    # another process would erase our batches from the file: refused while br2 holds it
    assert _state_child(state, tmp_path / "ledger_n10.jsonl") == 3
    br2.close()
    assert _state_child(state, tmp_path / "ledger_n10.jsonl") == 0  # free once closed
