"""Shared utilities: timestamps and atomic file writes."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def now_utc() -> str:
    """Current time in UTC as an ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()


def now_local() -> str:
    """Current time in the local timezone as an ISO-8601 string."""
    return datetime.now().astimezone().isoformat()


def atomic_write(path: Path, data: bytes) -> None:
    """Atomically write *data* to *path* (random tmp file + os.replace, cleanup on failure)."""
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


def atomic_write_json(path: Path, data: Any) -> None:
    """Atomically write *data* as formatted JSON (ensure_ascii=False, indent=2) to *path*."""
    atomic_write(path, json.dumps(data, ensure_ascii=False, indent=2).encode())
