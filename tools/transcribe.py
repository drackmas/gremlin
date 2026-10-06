"""Audio transcription tools: run long transcriptions as background jobs.

``transcribe_audio`` starts a faster-whisper transcription of a local audio or
video file in a separate subprocess (so multi-hour files never block the
agent's event loop or hold the GIL) and returns a job id immediately. A
background monitor (see :mod:`tools.job_monitor`) announces the result
back to the user's session when the job finishes; ``transcribe_status`` and
``kill_transcription`` let the model inspect or stop a job on demand. The
transcript is written to ``files/<name>-transcription.txt``.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

from config import AppConfig
from utils import atomic_write_text

from .registry import SESSION_ID, Tool

#: Whisper models present on disk and supported by this tool.
MODELS = ("turbo", "large-v3")
#: turbo is the default: near-large-v3 accuracy at a fraction of the runtime.
DEFAULT_MODEL = "turbo"


def _state_path(cfg: AppConfig, job_id: str) -> Path:
    return cfg.transcribe_jobs_dir / f"{job_id}.json"


def _load_job(cfg: AppConfig, job_id: str) -> dict | None:
    p = _state_path(cfg, job_id)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _write_job(cfg: AppConfig, job_id: str, updates: dict) -> None:
    job = _load_job(cfg, job_id) or {"id": job_id}
    job.update(updates)
    atomic_write_text(_state_path(cfg, job_id), json.dumps(job, ensure_ascii=False, indent=2))


def _resolve_input(cfg: AppConfig, path: str) -> Path:
    p = Path(path).expanduser()
    if not p.is_absolute():
        p = cfg.root / p
    return p


def _fmt_secs(n: float) -> str:
    n = int(n)
    h, rem = divmod(n, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m}m {s}s"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


def build_transcribe_tool(cfg: AppConfig) -> list[Tool]:
    cfg.transcribe_jobs_dir.mkdir(parents=True, exist_ok=True)
    cfg.files_dir.mkdir(parents=True, exist_ok=True)

    # ---- transcribe_audio ------------------------------------------------
    def transcribe_audio(args: dict) -> str:
        path = _resolve_input(cfg, args.get("path", ""))
        model = args.get("model", DEFAULT_MODEL)
        language = args.get("language", "en")
        overwrite = bool(args.get("overwrite", False))

        if not path.exists() or not path.is_file():
            return f"ERROR: input file not found: {path}"
        if model not in MODELS:
            return f"ERROR: unsupported model {model!r} (choose from {', '.join(MODELS)})"
        model_path = cfg.stt_dir / model
        if not (model_path / "model.bin").exists():
            return f"ERROR: model not found on disk: {model_path}"

        out_path = cfg.files_dir / (path.stem + "-transcription.txt")
        if out_path.exists() and not overwrite:
            return (
                f"ERROR: {out_path} already exists. Pass overwrite=true to replace it "
                f"or choose a different input."
            )

        job_id = uuid.uuid4().hex[:12]
        state = {
            "id": job_id,
            "session_id": SESSION_ID.get(),
            "input_path": str(path),
            "output_path": str(out_path),
            "model": model,
            "language": language,
            "status": "running",
            "started_at": time.time(),
            "pid": None,
            "duration_sec": 0.0,
            "current_offset_sec": 0.0,
            "eta_sec": None,
            "error": None,
            "finished_at": None,
            "notified": False,
        }
        _write_job(cfg, job_id, state)

        proc = subprocess.Popen(
            [
                sys.executable, "-m", "tools.transcribe_worker",
                str(path), str(model_path), language, str(out_path),
                str(_state_path(cfg, job_id)),
            ],
            cwd=str(cfg.root),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        _write_job(cfg, job_id, {"pid": proc.pid})

        return json.dumps({
            "job_id": job_id,
            "status": "running",
            "model": model,
            "output_path": str(out_path),
            "note": "Transcription started in the background. I'll tell the user the moment "
                    "it finishes - do not poll transcribe_status in a loop to wait for it.",
        })

    # ---- transcribe_status -----------------------------------------------
    def transcribe_status(args: dict) -> str:
        job = _load_job(cfg, args.get("job_id", ""))
        if job is None:
            return f"ERROR: unknown job id {args.get('job_id')!r}"
        status = job.get("status")
        if status == "running":
            dur = job.get("duration_sec") or 0.0
            off = job.get("current_offset_sec") or 0.0
            eta = job.get("eta_sec")
            if dur > 0:
                pct = min(100.0, off / dur * 100.0)
                line = f"running: {_fmt_secs(off)} of {_fmt_secs(dur)} ({pct:.0f}%)"
                if eta:
                    line += f", ~{_fmt_secs(eta)} left"
            else:
                line = "running: warming up (model loading / decoding)"
            return json.dumps({"job_id": job["id"], "status": status, "progress": line,
                               "output_path": job.get("output_path"),
                               "note": "Still running. Do NOT call transcribe_status again to wait - "
                                       "I will tell the user automatically the moment it finishes. "
                                       "Continue with other work or end your turn."})
        return json.dumps({
            "job_id": job["id"], "status": status,
            "output_path": job.get("output_path"), "error": job.get("error"),
        })

    # ---- kill_transcription ----------------------------------------------
    def kill_transcription(args: dict) -> str:
        job = _load_job(cfg, args.get("job_id", ""))
        if job is None:
            return f"ERROR: unknown job id {args.get('job_id')!r}"
        if job.get("status") != "running":
            return json.dumps({"job_id": job["id"], "status": job.get("status"),
                               "note": "not running; nothing to stop"})
        pid = job.get("pid")
        if not pid:
            return json.dumps({"job_id": job["id"], "status": job.get("status"),
                               "note": "no worker pid recorded"})
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            _write_job(cfg, job["id"], {"status": "killed", "finished_at": time.time()})
            return json.dumps({"job_id": job["id"], "status": "killed",
                               "note": "worker already exited"})
        except OSError as e:
            return f"ERROR: could not stop job: {e}"
        return json.dumps({"job_id": job["id"], "status": "stopping",
                           "note": "stop requested; it will be marked killed shortly"})

    return [
        Tool(
            name="transcribe_audio",
            description=(
                "Transcribe a local audio or video file to text using faster-whisper, "
                "run as a background job so long (even multi-hour) files don't block. "
                "Returns immediately with a job id; Gremlin tells the user when it's done. "
                "The transcript is saved to files/<name>-transcription.txt. "
                "Use for podcasts, interviews, lectures, videos, or any long recording."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string",
                             "description": "path to the audio/video file to transcribe"},
                    "model": {"type": "string", "enum": list(MODELS),
                              "description": "whisper model: 'turbo' (fast, default) or 'large-v3' (max accuracy)"},
                    "language": {"type": "string",
                                 "description": "spoken language code (e.g. 'en'); use 'auto' to detect"},
                    "overwrite": {"type": "boolean",
                                  "description": "replace the .txt if it already exists (default false)"},
                },
                "required": ["path"],
            },
            handler=transcribe_audio,
        ),
        Tool(
            name="transcribe_status",
            description=(
                "Check progress or result of a transcription job started by "
                "transcribe_audio. Call it at most once per job - do NOT poll "
                "it in a loop to wait. Gremlin tells the user automatically when "
                "the transcription completes, so if this returns 'running', stop "
                "checking and continue other work or end your turn."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "job_id": {"type": "string",
                               "description": "job id returned by transcribe_audio"},
                },
                "required": ["job_id"],
            },
            handler=transcribe_status,
        ),
        Tool(
            name="kill_transcription",
            description="Stop a running transcription job started by transcribe_audio.",
            parameters={
                "type": "object",
                "properties": {
                    "job_id": {"type": "string",
                               "description": "job id returned by transcribe_audio"},
                },
                "required": ["job_id"],
            },
            handler=kill_transcription,
        ),
    ]
