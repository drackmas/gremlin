"""Background worker for the audio transcription tool.

Runs as a subprocess so multi-hour transcriptions never block the agent's
event loop or hold the GIL. Invoked with five positional args::

    python -m tools.transcribe_worker <input> <model_path> <language> <output> <state>

It decodes *input* with faster-whisper (its bundled ffmpeg), streams
segments, writes the transcript to *output* incrementally, and keeps the
JSON *state* file current (offset / duration / ETA) so the main process can
report live progress to the user. Terminal status is ``done`` / ``error`` /
``killed``.
"""

from __future__ import annotations

import json
import os
import signal
import sys
import time
from pathlib import Path

from utils import atomic_write_text

#: Write the state file at most once per this many real seconds (plus when the
#: audio offset has advanced by :data:`OFFSET_STEP_SEC`).
WRITE_MIN_SEC = 5.0
#: Also refresh the state file once the audio offset advances this far.
OFFSET_STEP_SEC = 30.0


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


def main(argv: list[str]) -> int:
    if len(argv) != 5:
        print(__doc__, file=sys.stderr)
        return 2
    input_path = Path(argv[0])
    model_path = Path(argv[1])
    language = argv[2]
    output_path = Path(argv[3])
    state_path = Path(argv[4])

    signal.signal(signal.SIGTERM, _sigterm)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.parent.mkdir(parents=True, exist_ok=True)

    started_at = time.time()
    _save_state(state_path, {
        "status": "running", "error": None, "finished_at": None,
        "current_offset_sec": 0.0, "duration_sec": 0.0, "eta_sec": None,
    })

    if not input_path.exists():
        _save_state(state_path, {"status": "error", "error": f"input not found: {input_path}"})
        return 1

    try:
        from faster_whisper import WhisperModel
    except ImportError as e:  # pragma: no cover - environment guard
        _save_state(state_path, {"status": "error", "error": f"faster-whisper not installed: {e}"})
        return 1

    try:
        model = WhisperModel(str(model_path), device="cpu", compute_type="int8")
    except Exception as e:
        _save_state(state_path, {"status": "error", "error": f"model load failed: {e}"})
        return 1

    lang = None if language in ("", "auto") else language
    try:
        segments, info = model.transcribe(str(input_path), language=lang, vad_filter=True, beam_size=1)
    except Exception as e:
        _save_state(state_path, {"status": "error", "error": f"transcribe start failed: {e}"})
        return 1

    total = float(getattr(info, "duration", 0.0) or 0.0)
    last_write = 0.0
    last_offset = 0.0
    try:
        with output_path.open("w", encoding="utf-8") as out:
            for seg in segments:
                out.write(seg.text.strip() + "\n")
                if _KILLED["flag"]:
                    break
                offset = float(seg.end)
                now = time.time()
                if total > 0 and (offset - last_offset >= OFFSET_STEP_SEC or now - last_write >= WRITE_MIN_SEC):
                    frac = min(1.0, offset / total)
                    elapsed = now - started_at
                    eta = (elapsed / frac - elapsed) if frac > 0.05 else None
                    _save_state(state_path, {
                        "status": "running",
                        "current_offset_sec": offset,
                        "duration_sec": total,
                        "eta_sec": round(eta, 1) if eta is not None else None,
                    })
                    out.flush()
                    last_write = now
                    last_offset = offset
            out.flush()
    except Exception as e:
        _save_state(state_path, {"status": "error", "error": f"transcription failed: {e}"})
        return 1

    if _KILLED["flag"]:
        _save_state(state_path, {"status": "killed", "finished_at": time.time(), "eta_sec": None})
        print("killed", file=sys.stderr)
        return 130

    _save_state(state_path, {
        "status": "done", "finished_at": time.time(),
        "current_offset_sec": total, "duration_sec": total, "eta_sec": None,
        "output_path": str(output_path), "error": None,
    })
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
