"""Durable append-only JSONL storage, plus a canonical prompt hash.

Each ``append`` writes one JSON line, flushes and calls ``os.fsync`` so a crash
loses at most the line being written. A truncated last line (crash mid-write)
is detected, reported with a warning and cut off before the next append.
Files are UTF-8; paths may contain spaces (Windows-safe, uses pathlib).
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import shutil
import threading
import warnings
from pathlib import Path
from typing import Any, Iterator, Mapping


def canonical_json(obj: Any) -> str:
    """Deterministic JSON: sorted keys, no spaces, UTF-8 kept as is."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def prompt_hash(system: Any, messages: Any) -> str:
    """sha256 of the canonical JSON of (system, messages).

    Lets a run log a short hash plus the variables that filled a template,
    instead of storing every full prompt.
    """
    payload = canonical_json({"system": system, "messages": messages})
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class JsonlStore:
    """Append-only JSONL file keyed by a field (default ``"key"``)."""

    def __init__(self, path: str | os.PathLike[str], key_field: str = "key") -> None:
        self.path = Path(path)
        self.key_field = key_field
        self._lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._repaired = False
        self.repair()

    # ------------------------------------------------------------------ repair
    def repair(self) -> bool:
        """Cut off a truncated last line. Returns True if the file was changed."""
        with self._lock:
            changed = self._repair_locked()
            self._repaired = True
            return changed

    def _repair_locked(self) -> bool:
        if not self.path.exists():
            return False
        size = self.path.stat().st_size
        if size == 0:
            return False
        with open(self.path, "rb+") as f:
            f.seek(size - 1)
            if f.read(1) == b"\n":
                return False
            # Find the start of the last (unterminated) line.
            block = 1 << 16
            pos = size
            last_nl = -1
            while pos > 0 and last_nl < 0:
                start = max(0, pos - block)
                f.seek(start)
                chunk = f.read(pos - start)
                idx = chunk.rfind(b"\n")
                if idx >= 0:
                    last_nl = start + idx
                pos = start
            cut = last_nl + 1  # 0 when the whole file is one unterminated line
            f.seek(cut)
            tail = f.read()
            try:
                json.loads(tail.decode("utf-8"))
                complete = True
            except (UnicodeDecodeError, json.JSONDecodeError):
                complete = False
            if complete:
                # A whole record whose newline was lost: keep it, add the newline.
                f.seek(size)
                f.write(b"\n")
            else:
                warnings.warn(
                    f"{self.path}: dropping truncated last line ({len(tail)} bytes) "
                    "left by an interrupted write",
                    RuntimeWarning,
                    stacklevel=3,
                )
                f.truncate(cut)
            f.flush()
            os.fsync(f.fileno())
        return True

    # ------------------------------------------------------------------ write
    def append(self, record: Mapping[str, Any]) -> None:
        """Append one record as a JSON line, then flush and fsync."""
        try:
            data = (json.dumps(dict(record), ensure_ascii=False, default=str) + "\n").encode("utf-8")
        except UnicodeEncodeError:
            # A lone UTF-16 surrogate (model text cut mid-emoji, for example) has no
            # UTF-8 form. \u-escaping it keeps the line valid JSON, and json.loads
            # gives back the same string, so one odd record cannot stop a run.
            data = (json.dumps(dict(record), ensure_ascii=True, default=str) + "\n").encode("ascii")
        with self._lock:
            if not self._repaired:
                self._repair_locked()
                self._repaired = True
            with open(self.path, "ab") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())

    # ------------------------------------------------------------------ read
    def iter_records(self) -> Iterator[dict[str, Any]]:
        """Yield every parseable record. Bad lines are skipped with a warning."""
        if not self.path.exists():
            return
        with open(self.path, "r", encoding="utf-8", errors="replace", newline="\n") as f:
            for lineno, line in enumerate(f, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    warnings.warn(
                        f"{self.path}:{lineno}: skipping unparseable line", RuntimeWarning, stacklevel=2
                    )
                    continue
                if isinstance(rec, dict):
                    yield rec

    def done_keys(self, include_errors: bool = False) -> set[str]:
        """Keys already stored. Records with ``status == "error"`` are excluded
        by default so that failed tasks are retried on resume."""
        keys: set[str] = set()
        for rec in self.iter_records():
            if not include_errors and rec.get("status") == "error":
                continue
            k = rec.get(self.key_field)
            if k is not None:
                keys.add(str(k))
        return keys

    def __len__(self) -> int:
        return sum(1 for _ in self.iter_records())

    # ------------------------------------------------------------------ export
    def export_gz(self, dest: str | os.PathLike[str] | None = None) -> Path:
        """Write a gzip copy of the file (default: same path + ``.gz``)."""
        out = Path(dest) if dest is not None else self.path.with_name(self.path.name + ".gz")
        out.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            with open(self.path, "rb") as src, gzip.open(out, "wb") as dst:
                shutil.copyfileobj(src, dst)
        return out
