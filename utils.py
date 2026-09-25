"""Shared utilities: timestamps and atomic, optionally-locked file writes."""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator
def now_utc() -> str:
    """Current time in UTC as an ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()


def now_local() -> str:
    """Current time in the local timezone as an ISO-8601 string."""
    return datetime.now().astimezone().isoformat()

# fcntl is POSIX-only; on other platforms locking degrades to a no-op.
with contextlib.suppress(ImportError):
    import fcntl


@contextlib.contextmanager
def file_lock(path: Path) -> Iterator[None]:
    """Hold an exclusive advisory lock on the sibling ``<path>.lock`` file.

    The lock file is created if needed and left in place (unlinking it would
    reintroduce the create-then-lock race). On platforms without ``fcntl`` the
    body yields immediately, so callers stay portable.

    >>> import utils, tempfile, pathlib
    """
    lock_path = path.with_name(path.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    if fcntl is None:  # pragma: no cover - non-POSIX fallback
        yield
        return
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def atomic_write(path: Path, data: bytes) -> None:
    """Atomically write *data* to *path* under an exclusive file lock.

    A random temp file in the same directory is filled then ``os.replace``'d
    into place, so readers never observe a partial write. An advisory lock
    serializes concurrent writers (e.g. two processes touching one store).
    """
    with file_lock(path):
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise


def atomic_write_text(path: Path, text: str) -> None:
    """Atomically write *text* (UTF-8) to *path* under the file lock."""
    atomic_write(path, text.encode("utf-8"))


def atomic_write_json(path: Path, data: Any) -> None:
    """Atomically write *data* as formatted JSON (ensure_ascii=False, indent=2)."""
    atomic_write(path, json.dumps(data, ensure_ascii=False, indent=2).encode())
