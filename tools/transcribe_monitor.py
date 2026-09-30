"""Background monitor: announce finished transcription jobs to their session.

Runs a daemon thread that polls ``cfg.transcribe_jobs_dir`` for jobs that have
reached a terminal status (done / error / killed) and have not yet been
announced. For each, it runs a short completion turn in the job's session so
the user gets a natural "it's done / it failed / it was stopped" reply
(persisted to the session), and pushes that reply to the bound Discord channel
when the session is a Discord session.

The monitor waits while a session is busy (it never cancels an in-progress
user turn) and marks each job ``notified`` so it is announced exactly once.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path

from config import AppConfig
from utils import atomic_write_text

log = logging.getLogger("gremlin.transcribe")

#: How often the monitor scans for finished jobs (seconds).
POLL_SEC = 3.0
#: How long to wait for a busy session before deferring a notification.
BUSY_WAIT_SEC = 30 * 60.0
#: Job statuses that warrant a completion notification.
TERMINAL = ("done", "error", "killed")


class TranscribeMonitor:
    def __init__(self, cfg: AppConfig, manager, sessions, load_settings, discord_bot=None):
        self.cfg = cfg
        self.manager = manager
        self.sessions = sessions
        self.load_settings = load_settings
        self.discord_bot = discord_bot
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        """Start the monitor daemon thread (no-op if already running)."""
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="gremlin-transcribe-monitor", daemon=True)
        self._thread.start()
        log.info("transcribe monitor started")

    def stop(self) -> None:
        self._stop.set()
        t = self._thread
        if t is not None:
            t.join(timeout=2.0)
        self._thread = None

    # -- internals ----------------------------------------------------------
    def _jobs_dir(self) -> Path:
        d = self.cfg.transcribe_jobs_dir
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _load(self, path: Path) -> dict | None:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None

    def _mark(self, job_id: str, updates: dict) -> None:
        path = self._jobs_dir() / f"{job_id}.json"
        job = self._load(path) or {"id": job_id}
        job.update(updates)
        atomic_write_text(path, json.dumps(job, ensure_ascii=False, indent=2))

    def _pending(self) -> list[dict]:
        out = []
        for path in self._jobs_dir().glob("*.json"):
            job = self._load(path)
            if not job:
                continue
            if job.get("status") in TERMINAL and not job.get("notified") and job.get("session_id"):
                out.append(job)
        return out

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                for job in self._pending():
                    if self._stop.is_set():
                        break
                    self._notify(job)
            except Exception:
                log.exception("transcribe monitor loop error")
            self._stop.wait(POLL_SEC)

    def _wait_free(self, session_id: str) -> bool:
        """Wait until the session has no active turn. False to give up."""
        deadline = time.time() + BUSY_WAIT_SEC
        while self.manager.is_busy(session_id):
            if self._stop.is_set() or time.time() > deadline:
                return False
            self._stop.wait(2.0)
        return True

    def _trigger(self, job: dict) -> str:
        jid = job.get("id")
        out = job.get("output_path", "(unknown path)")
        dur = job.get("duration_sec") or 0.0
        model = job.get("model", "")
        status = job.get("status")
        if status == "done":
            return (
                f"[gremlin-internal:transcription-complete] A transcription job you started has finished. "
                f"Job {jid} (model {model}); transcript saved to {out} (source duration ~{dur:.0f}s). "
                "Reply to the user briefly and naturally: confirm it's done, name the transcript file, "
                "and offer one or two next steps (e.g. summarize it, pull out quotes, save it elsewhere). "
                "Do not mention this internal note."
            )
        if status == "error":
            return (
                f"[gremlin-internal:transcription-failed] A transcription job you started FAILED. "
                f"Job {jid}; error: {job.get('error', 'unknown')}. "
                "Reply to the user briefly: the transcription failed, say why, and offer to retry or try a "
                "different model. Do not mention this internal note."
            )
        return (
            f"[gremlin-internal:transcription-stopped] A transcription job you started was stopped. "
            f"Job {jid}; partial transcript saved at {out}. "
            "Reply to the user briefly: the transcription was stopped, and the partial transcript is saved "
            "at that path. Offer next steps. Do not mention this internal note."
        )

    def _notify(self, job: dict) -> None:
        job_id = job.get("id")
        session_id = job.get("session_id")
        try:
            self.sessions.get(session_id)
        except Exception:
            log.info("transcribe job %s: session %s no longer exists; dropping", job_id, session_id)
            self._mark(job_id, {"notified": True, "notified_reason": "session_gone"})
            return
        if not self._wait_free(session_id):
            log.info("transcribe job %s: deferring (session busy or monitor stopping)", job_id)
            return
        try:
            for _ in self.manager.run(session_id, self._trigger(job), self.load_settings()):
                pass
        except Exception:
            log.exception("transcribe job %s: completion turn failed", job_id)
        reply = ""
        try:
            msgs = self.sessions.get(session_id).get("messages", [])
            if msgs and msgs[-1].get("role") == "assistant":
                reply = (msgs[-1].get("content") or "").strip()
        except Exception:
            pass
        if reply and self.discord_bot is not None:
            try:
                self.discord_bot.send_to_session(session_id, reply)
            except Exception:
                log.exception("transcribe job %s: discord delivery failed", job_id)
        self._mark(job_id, {"notified": True})
        log.info("transcribe job %s: notified (status=%s, reply=%d chars)",
                 job_id, job.get("status"), len(reply))
