"""Background worker for the YouTube download tools.

Runs as a detached subprocess so long downloads never block the agent's
event loop or hold the GIL. Invoked with one positional arg::

    python -m tools.download_worker <state_path>

It reads the job state JSON, runs the yt-dlp download
(:func:`tools.youtube.run_download`), and keeps the state file current
(``progress_pct`` while downloading). Terminal status is ``done`` /
``error`` / ``killed``; the background monitor
(see :mod:`tools.job_monitor`) announces the result to the user's session.
"""

from __future__ import annotations

import json
import signal
import sys
import time
from pathlib import Path

from utils import atomic_write_text

#: Write the state file at most once per this many real seconds while downloading.
WRITE_MIN_SEC = 2.0


def _load_state(state_path: Path) -> dict:
    try:
        return json.loads(state_path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_state(state_path: Path, updates: dict) -> None:
    state = _load_state(state_path)
    state.update(updates)
    atomic_write_text(state_path, json.dumps(state, ensure_ascii=False, indent=2))


_KILLED = {"flag": False}


def _sigterm(signum, _frame):  # pragma: no cover - signal path
    _KILLED["flag"] = True
    raise SystemExit(128 + signum)


def _progress_hook(state_path: Path):
    """yt-dlp progress hook: throttle ``progress_pct`` updates to the state file."""
    last = {"t": 0.0}

    def hook(d: dict) -> None:
        if d.get("status") != "downloading":
            return
        now = time.time()
        if now - last["t"] < WRITE_MIN_SEC:
            return
        last["t"] = now
        total = d.get("total_bytes") or d.get("total_bytes_estimate")
        if not total:
            return
        _save_state(state_path, {"progress_pct": min(100.0, d.get("downloaded_bytes", 0) / total * 100.0)})

    return hook

def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print(f"usage: {argv[0]} <state_path>", file=sys.stderr)
        return 2
    signal.signal(signal.SIGTERM, _sigterm)

    state_path = Path(argv[0])
    job = _load_state(state_path)
    url = job.get("url", "")
    kind = job.get("kind", "video")
    stem = job.get("stem", "")
    dest_dir = Path(job.get("dest_dir", ""))
    if not (url and stem and str(dest_dir)):
        _save_state(state_path, {"status": "error", "error": "job state is missing url/stem/dest_dir",
                                 "finished_at": time.time()})
        return 1

    from tools.youtube import _download_opts, run_download

    extra = _download_opts(kind)
    extra["progress_hooks"] = [_progress_hook(state_path)]

    try:
        result = run_download(url, dest_dir, stem, extra)
    except Exception as e:
        if _KILLED["flag"]:
            _save_state(state_path, {"status": "killed", "finished_at": time.time()})
        else:
            _save_state(state_path, {"status": "error", "error": str(e), "finished_at": time.time()})
        return 1

    _save_state(state_path, {
        "status": "done",
        "output_path": str(result),
        "size_bytes": result.stat().st_size,
        "progress_pct": 100.0,
        "finished_at": time.time(),
    })
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
