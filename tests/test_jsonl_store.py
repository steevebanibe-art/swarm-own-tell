import gzip
import json

import pytest

from harness.jsonl_store import JsonlStore, prompt_hash


def test_append_and_read_utf8_path_with_spaces(tmp_path):
    p = tmp_path / "a folder with spaces" / "réponses.jsonl"
    s = JsonlStore(p)
    s.append({"key": "k1", "text": "Bonjour, ça va ? 你好"})
    s.append({"key": "k2", "status": "error"})
    recs = list(s.iter_records())
    assert recs[0]["text"] == "Bonjour, ça va ? 你好"
    assert s.done_keys() == {"k1"}
    assert s.done_keys(include_errors=True) == {"k1", "k2"}
    assert p.read_bytes().endswith(b"\n")


def test_lone_surrogate_is_escaped_not_fatal(tmp_path):
    p = tmp_path / "x.jsonl"
    s = JsonlStore(p)
    cut = json.loads('"emoji cut \\ud83d"')  # what json.loads gives for a lone surrogate
    s.append({"key": "k1", "text": cut})
    s.append({"key": "k2", "text": "é"})
    recs = list(s.iter_records())
    assert recs[0]["text"] == cut
    assert recs[1]["text"] == "é"
    p.read_bytes().decode("utf-8")  # the file stays valid UTF-8


def test_truncated_last_line_is_repaired(tmp_path):
    p = tmp_path / "x.jsonl"
    p.write_bytes(b'{"key": "a"}\n{"key": "b"}\n{"key": "c", "va')
    with pytest.warns(RuntimeWarning, match="truncated"):
        s = JsonlStore(p)
    assert s.done_keys() == {"a", "b"}
    s.append({"key": "d"})
    assert [r["key"] for r in s.iter_records()] == ["a", "b", "d"]
    assert p.read_text(encoding="utf-8").count("\n") == 3


def test_complete_last_line_without_newline_is_kept(tmp_path):
    p = tmp_path / "x.jsonl"
    p.write_bytes(b'{"key": "a"}\n{"key": "b"}')
    s = JsonlStore(p)
    s.append({"key": "c"})
    assert [r["key"] for r in s.iter_records()] == ["a", "b", "c"]


def test_single_truncated_line(tmp_path):
    p = tmp_path / "x.jsonl"
    p.write_bytes(b'{"key": "a", "x')
    with pytest.warns(RuntimeWarning):
        s = JsonlStore(p)
    assert len(s) == 0
    s.append({"key": "z"})
    assert s.done_keys() == {"z"}


def test_export_gz(tmp_path):
    s = JsonlStore(tmp_path / "x.jsonl")
    s.append({"key": "a"})
    out = s.export_gz()
    assert out.name == "x.jsonl.gz"
    with gzip.open(out, "rt", encoding="utf-8") as f:
        assert json.loads(f.readline())["key"] == "a"


def test_prompt_hash_is_canonical():
    m1 = [{"role": "user", "content": "hi"}]
    m2 = [{"content": "hi", "role": "user"}]
    assert prompt_hash("sys", m1) == prompt_hash("sys", m2)
    assert prompt_hash("sys", m1) != prompt_hash("sys2", m1)
    assert len(prompt_hash(None, m1)) == 64


def test_failed_write_is_repaired_before_next_append(tmp_path, monkeypatch):
    import builtins

    p = tmp_path / "x.jsonl"
    s = JsonlStore(p)
    s.append({"key": "a"})
    real_open = builtins.open

    class HalfWrite:
        """A file whose write stores half the line, then fails (e.g. disk full)."""

        def __init__(self, f):
            self.f = f

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.f.close()

        def write(self, data):
            self.f.write(data[: len(data) // 2])
            self.f.flush()
            raise OSError("disk full")

    monkeypatch.setattr(builtins, "open", lambda *a, **k: HalfWrite(real_open(*a, **k)))
    with pytest.raises(OSError, match="disk full"):
        s.append({"key": "b", "text": "some longer text"})
    monkeypatch.setattr(builtins, "open", real_open)
    with pytest.warns(RuntimeWarning, match="truncated"):
        s.append({"key": "c"})
    assert [r["key"] for r in s.iter_records()] == ["a", "c"]


# ---------------------------------------------------------------- several processes, one file
import subprocess  # noqa: E402
import sys  # noqa: E402
from pathlib import Path  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
_APPENDER = (
    "import sys; sys.path.insert(0, sys.argv[1])\n"
    "from harness.jsonl_store import JsonlStore\n"
    "s = JsonlStore(sys.argv[2])\n"
    "who, n = sys.argv[3], int(sys.argv[4])\n"
    "pad = 'x' * 3000\n"
    "for i in range(n):\n"
    "    s.append({'key': f'{who}-{i}', 'pad': pad})\n"
)


def test_concurrent_appends_from_several_processes_lose_nothing(tmp_path):
    p = tmp_path / "shared.jsonl"
    n, who = 400, ("p0", "p1", "p2")
    procs = [
        subprocess.Popen([sys.executable, "-c", _APPENDER, str(ROOT), str(p), w, str(n)]) for w in who
    ]
    assert all(proc.wait(timeout=25) == 0 for proc in procs)
    lines = p.read_bytes().split(b"\n")
    assert lines[-1] == b""
    keys = [json.loads(l)["key"] for l in lines[:-1]]  # every line parses
    assert len(keys) == len(who) * n
    assert set(keys) == {f"{w}-{i}" for w in who for i in range(n)}


def test_append_waits_for_the_cross_process_lock(tmp_path):
    from harness.filelock import AppendLock

    p = tmp_path / "x.jsonl"
    s = JsonlStore(p, lock_timeout_s=0.2)
    other = AppendLock(s.lock_path)  # another handle, as another process would have
    with other:
        with pytest.raises(TimeoutError):
            s.append({"key": "a"})
    s.append({"key": "b"})
    assert s.done_keys() == {"b"}
