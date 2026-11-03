"""Small cross-process file locks (Windows msvcrt / POSIX flock), no dependency.

Two kinds:

* ``os_lock`` / ``os_unlock``: non-blocking exclusive lock on byte 0 of an
  open lock file; ``OSError`` if another handle holds it. Used for the
  "one process per file" locks (ledger, batch state file).
* ``AppendLock``: a short blocking lock on a sidecar file, taken around one
  write and released at once, so several processes (or several objects in
  one process) can append to the same file without overwriting each other.

An OS lock is released by the OS when its handle is closed or the process
dies, including a crash, so a stale lock file never blocks a later run.
"""

from __future__ import annotations

import os
import threading
import time
import weakref
from pathlib import Path


def os_lock(fd: int) -> None:
    """Non-blocking exclusive lock on byte 0 of ``fd``; OSError if already held."""
    if os.name == "nt":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    else:  # pragma: no cover - POSIX only
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def os_unlock(fd: int) -> None:
    if os.name == "nt":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:  # pragma: no cover - POSIX only
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_UN)


class AppendLock:
    """Blocking, cross-process exclusive lock on ``lock_path``, held only briefly.

    ``with lock:`` waits (polling every few milliseconds) until no other handle
    holds the lock, then holds it for the body. ``TimeoutError`` after
    ``timeout_s``. Also serializes threads of this process.
    """

    def __init__(self, lock_path: str | os.PathLike[str], timeout_s: float = 60.0) -> None:
        self.path = Path(lock_path)
        self.timeout_s = float(timeout_s)
        self._thread_lock = threading.Lock()
        self._fd: int | None = None
        self._finalizer: weakref.finalize | None = None

    def _open(self) -> int:
        if self._fd is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._fd = os.open(str(self.path), os.O_RDWR | os.O_CREAT, 0o644)
            self._finalizer = weakref.finalize(self, os.close, self._fd)
        return self._fd

    def acquire(self) -> None:
        if not self._thread_lock.acquire(timeout=self.timeout_s if self.timeout_s > 0 else -1):
            raise TimeoutError(f"could not lock {self.path} within {self.timeout_s}s")
        try:
            fd = self._open()
            deadline = time.monotonic() + self.timeout_s
            delay = 0.001
            while True:
                try:
                    os_lock(fd)
                    return
                except OSError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError(
                            f"could not lock {self.path} within {self.timeout_s}s "
                            "(another process keeps it locked)"
                        ) from None
                    time.sleep(delay)
                    delay = min(0.05, delay * 2)
        except BaseException:
            self._thread_lock.release()
            raise

    def release(self) -> None:
        try:
            if self._fd is not None:
                os_unlock(self._fd)
        finally:
            self._thread_lock.release()

    def __enter__(self) -> "AppendLock":
        self.acquire()
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()

    def close(self) -> None:
        if self._finalizer is not None:
            self._finalizer()
        self._fd = None


class ProcessFileLock:
    """Exclusive "one process per file" lock on ``lock_path``, held until ``close()``.

    Several holders inside one process share the OS lock (reference counted);
    another process gets ``error_type(message)``. The OS lock is dropped when
    the last holder closes, is garbage collected, or the process exits.
    """

    _registry: dict[str, list] = {}  # id -> [fd, refs]
    _guard = threading.Lock()

    def __init__(self, lock_path: str | os.PathLike[str], error_type: type[Exception], message: str) -> None:
        path = Path(lock_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        lid = os.path.normcase(str(path.resolve()))
        with ProcessFileLock._guard:
            slot = ProcessFileLock._registry.get(lid)
            if slot is None:
                fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o644)
                try:
                    os_lock(fd)
                except OSError:
                    os.close(fd)
                    raise error_type(message) from None
                slot = [fd, 0]
                ProcessFileLock._registry[lid] = slot
            slot[1] += 1
        self.path = path
        self._finalizer = weakref.finalize(self, ProcessFileLock._drop, lid)

    @staticmethod
    def _drop(lid: str) -> None:
        with ProcessFileLock._guard:
            slot = ProcessFileLock._registry.get(lid)
            if slot is None:
                return
            slot[1] -= 1
            if slot[1] > 0:
                return
            del ProcessFileLock._registry[lid]
            try:
                os_unlock(slot[0])
            except OSError:
                pass
            os.close(slot[0])

    def close(self) -> None:
        self._finalizer()

    @property
    def closed(self) -> bool:
        return not self._finalizer.alive
