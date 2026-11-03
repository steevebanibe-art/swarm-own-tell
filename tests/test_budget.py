import asyncio
import random

import pytest

from harness.budget import BudgetExceeded, CostLedger
from tests.conftest import read_lines


def test_reserve_settle_release(tmp_path):
    led = CostLedger(tmp_path / "ledger.jsonl", hard_cap_usd=1.0)
    r = led.reserve(0.4)
    assert led.outstanding_usd == pytest.approx(0.4)
    led.settle(r, key="a", model="claude-haiku-5-5", usage={"input_tokens": 5}, cost_usd=0.1)
    assert led.spent_usd == pytest.approx(0.1)
    assert led.outstanding_usd == 0
    r2 = led.reserve(0.5)
    led.release(r2)
    assert led.outstanding_usd == 0
    lines = read_lines(tmp_path / "ledger.jsonl")
    assert len(lines) == 1
    assert set(lines[0]) >= {"ts", "key", "model", "usage", "cost_usd", "batch"}
    assert lines[0]["usage"]["input_tokens"] == 5


def test_cap_blocks(tmp_path):
    led = CostLedger(tmp_path / "l.jsonl", hard_cap_usd=1.0)
    led.reserve(0.7)
    with pytest.raises(BudgetExceeded):
        led.reserve(0.31)
    led.reserve(0.3)  # exactly at the cap is allowed


def test_replay_after_restart(tmp_path):
    p = tmp_path / "dir with spaces" / "ledger.jsonl"
    led = CostLedger(p, hard_cap_usd=1.0)
    for i in range(3):
        led.settle(led.reserve(0.2), key=f"k{i}", model="m", usage=None, cost_usd=0.25)
    led.close()  # the first process is gone
    # simulate a crash in the middle of a write
    with open(p, "ab") as f:
        f.write(b'{"ts": 1, "key": "k3", "cost_u')
    with pytest.warns(RuntimeWarning):
        led2 = CostLedger(p, hard_cap_usd=1.0)
    assert led2.spent_usd == pytest.approx(0.75)
    assert led2.has_key("k2") and not led2.has_key("k3")
    with pytest.raises(BudgetExceeded):
        led2.reserve(0.26)
    led2.settle(led2.reserve(0.25), key="k4", model="m", usage=None, cost_usd=0.25)
    assert len(read_lines(p)) == 4


async def test_concurrent_reservations_cannot_overshoot(tmp_path):
    cap = 1.0
    led = CostLedger(tmp_path / "l.jsonl", hard_cap_usd=cap)
    rng = random.Random(0)
    blocked = 0

    async def one(i: int) -> None:
        nonlocal blocked
        worst = 0.03
        try:
            r = led.reserve(worst)
        except BudgetExceeded:
            blocked += 1
            return
        await asyncio.sleep(rng.random() * 0.01)
        # the real cost is at most the worst case
        led.settle(r, key=f"k{i}", model="m", usage=None, cost_usd=worst * rng.uniform(0.5, 1.0))
        assert led.spent_usd + led.outstanding_usd <= cap + 1e-9

    await asyncio.gather(*(one(i) for i in range(200)))
    assert blocked > 0
    assert led.spent_usd <= cap
    assert led.outstanding_usd == 0


# ---------------------------------------------------------------- one process per ledger
import subprocess  # noqa: E402
import sys  # noqa: E402
from pathlib import Path  # noqa: E402

from harness.budget import LedgerLocked  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
_CHILD = (
    "import sys; sys.path.insert(0, sys.argv[1]);"
    "from harness.budget import BudgetExceeded, CostLedger, LedgerLocked\n"
    "try:\n"
    "    led = CostLedger(sys.argv[2], hard_cap_usd=1.0)\n"
    "except LedgerLocked:\n"
    "    sys.exit(3)\n"
    "try:\n"
    "    r = led.reserve(0.9)\n"
    "except BudgetExceeded:\n"
    "    sys.exit(4)\n"
    "led.settle(r, key='child', model='m', usage=None, cost_usd=0.9)\n"
)


def _child(path) -> int:
    return subprocess.run([sys.executable, "-c", _CHILD, str(ROOT), str(path)], timeout=25).returncode


def test_second_process_on_same_ledger_is_refused(tmp_path):
    p = tmp_path / "ledger.jsonl"
    led = CostLedger(p, hard_cap_usd=1.0)
    r = led.reserve(0.9)
    assert _child(p) == 3  # refused: it would have spent its own 0.9 on top of ours
    led.settle(r, key="parent", model="m", usage=None, cost_usd=0.9)
    assert len(read_lines(p)) == 1
    led.close()
    # free again once the holder is closed, and the child sees the 0.9 already spent
    assert _child(p) == 4
    assert len(read_lines(p)) == 1


def test_two_objects_in_one_process_share_the_cap(tmp_path):
    p = tmp_path / "ledger.jsonl"
    a = CostLedger(p, hard_cap_usd=1.0)
    b = CostLedger(p, hard_cap_usd=1.0)  # e.g. a notebook cell run twice
    ra = a.reserve(0.9)
    with pytest.raises(BudgetExceeded):
        b.reserve(0.9)
    a.settle(ra, key="a", model="m", usage=None, cost_usd=0.9)
    assert b.spent_usd == pytest.approx(0.9)
    with pytest.raises(BudgetExceeded):
        b.reserve(0.2)
    a.close()
    with pytest.raises(BudgetExceeded):  # b still holds the file and the state
        b.reserve(0.2)
    with pytest.raises(RuntimeError):
        a.reserve(0.01)  # a closed ledger refuses new reservations
    b.close()
    with CostLedger(p, hard_cap_usd=1.0) as c:  # replays from disk after the last close
        assert c.spent_usd == pytest.approx(0.9)


def test_forced_and_tagged_reservations(tmp_path):
    led = CostLedger(tmp_path / "l.jsonl", hard_cap_usd=1.0)
    led.reserve(0.8, tag="batch:x")
    led.reserve(0.8, tag="batch:x")  # same commitment registered twice: replaced, not doubled
    assert led.outstanding_usd == pytest.approx(0.8)
    led.reserve(0.5, force=True)  # already-committed money never raises
    assert led.outstanding_usd == pytest.approx(1.3) and led.remaining_usd < 0
    with pytest.raises(BudgetExceeded):
        led.reserve(0.01)
    led.settle(None, key="batch:x:a", model="m", usage=None, cost_usd=0.1)
    led.settle(None, key="batch:x:b", model="m", usage=None, cost_usd=0.2)
    led.settle(None, key="batch:y:a", model="m", usage=None, cost_usd=0.4)
    assert led.cost_with_prefix("batch:x:") == pytest.approx(0.3)
