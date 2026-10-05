"""Background monitor: announce finished background jobs to their session.

Runs a daemon thread that polls the job state directories
(``cfg.transcribe_jobs_dir`` and ``cfg.download_jobs_dir``) for jobs that
have reached a terminal status (done / error / killed) and have not yet been
announced. For each, it runs a short completion turn in the job's session so
the user gets a natural "it's done / it failed / it was stopped" reply
(persisted to the session), and pushes that reply to the bound Discord
channel when the session is a Discord session.

It also sweeps "running" jobs whose worker process is no longer alive
(crash, OOM, reboot): those are marked ``error`` so they are announced too,
instead of hanging "running" forever without any notification.

The monitor waits while a session is busy (it never cancels an in-progress
user turn) and marks each job ``notified`` so it is announced exactly once.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path

from config import AppConfig
from planning.store import PLAN_ACTIVE, PlanError, PlanStore, plan_progress
from utils import atomic_write_text

log = logging.getLogger("gremlin.jobs")

#: How often the monitor scans for finished jobs (seconds).
POLL_SEC = 3.0
#: How long to wait for a busy session before deferring a notification.
BUSY_WAIT_SEC = 30 * 60.0
#: A running job whose worker died is only flagged after this much wall time
#: (avoids the start-up race and pid-reuse false positives).
DEAD_GRACE_SEC = 60.0
#: Job statuses that warrant a completion notification.
TERMINAL = ("done", "error", "killed")


class JobMonitor:
    def __init__(self, cfg: AppConfig, manager, sessions, load_settings, discord_bot=None):
        self.cfg = cfg
        self.plans = PlanStore(Path(cfg.root) / "plan.json")
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
        self._thread = threading.Thread(target=self._loop, name="gremlin-job-monitor", daemon=True)
        self._thread.start()
        log.info("job monitor started")

    def stop(self) -> None:
        self._stop.set()
        t = self._thread
        if t is not None:
            t.join(timeout=2.0)
        self._thread = None

    # -- internals ----------------------------------------------------------
    def _sources(self) -> list[tuple[str, Path]]:
        """(kind, jobs_dir) pairs to watch."""
        return [
            ("transcribe", self.cfg.transcribe_jobs_dir),
            ("download", self.cfg.download_jobs_dir),
        ]

    def _load(self, path: Path) -> dict | None:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None

    def _mark(self, jobs_dir: Path, job_id: str, updates: dict) -> None:
        path = jobs_dir / f"{job_id}.json"
        job = self._load(path) or {"id": job_id}
        job.update(updates)
        atomic_write_text(path, json.dumps(job, ensure_ascii=False, indent=2))

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def _sweep_dead(self) -> None:
        """Flag running jobs whose worker process is gone (crash / OOM / reboot)."""
        now = time.time()
        for kind, jobs_dir in self._sources():
            jobs_dir.mkdir(parents=True, exist_ok=True)
            for path in jobs_dir.glob("*.json"):
                job = self._load(path)
                if not job or job.get("status") != "running" or job.get("notified"):
                    continue
                pid = job.get("pid")
                if not pid:
                    continue
                if now - (job.get("started_at") or 0) < DEAD_GRACE_SEC:
                    continue
                if self._pid_alive(pid):
                    continue
                self._mark(jobs_dir, job.get("id"), {
                    "status": "error",
                    "error": "worker process died without reporting (crashed or was killed)",
                    "finished_at": now,
                })
                log.info("job %s (%s): worker pid %s is dead; marked error",
                         job.get("id"), kind, pid)

    def _pending(self) -> list[tuple[str, dict]]:
        out: list[tuple[str, dict]] = []
        for kind, jobs_dir in self._sources():
            jobs_dir.mkdir(parents=True, exist_ok=True)
            for path in jobs_dir.glob("*.json"):
                job = self._load(path)
                if not job:
                    continue
                if job.get("status") in TERMINAL and not job.get("notified") and job.get("session_id"):
                    out.append((kind, job))
        return out

    def _scan(self) -> list[tuple[str, dict]]:
        """One poll pass: sweep dead workers, then notify finished jobs."""
        self._sweep_dead()
        notified: list[tuple[str, dict]] = []
        for kind, job in self._pending():
            if self._stop.is_set():
                break
            self._notify(kind, job)
            notified.append((kind, job))
        return notified

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._scan()
            except Exception:
                log.exception("job monitor loop error")
            self._stop.wait(POLL_SEC)

    def _wait_free(self, session_id: str) -> bool:
        """Wait until the session has no active turn. False to give up."""
        deadline = time.time() + BUSY_WAIT_SEC
        while self.manager.is_busy(session_id):
            if self._stop.is_set() or time.time() > deadline:
                return False
            self._stop.wait(2.0)
        return True

    def _active_plan(self) -> dict | None:
        """``{'remaining': n, 'total': m}`` when a usable plan is active; None
        when there is no active plan (missing, finished, or corrupt plan.json)."""
        try:
            plan = self.plans.load()
        except PlanError:
            return None
        if plan is None or plan.status != PLAN_ACTIVE:
            return None
        done, total, _ = plan_progress(plan)
        return {"remaining": total - done, "total": total}

    def _trigger(self, kind: str, job: dict) -> str:
        jid = job.get("id")
        out = job.get("output_path", "(unknown path)")
        plan = self._active_plan()
        if kind == "download":
            what = "video" if job.get("kind") == "video" else "audio"
            size = job.get("size_bytes")
            size_str = f" ({size / (1024 * 1024):.1f} MB)" if size else ""
            if job.get("status") == "done":
                base = (
                    f"[gremlin-internal:download-complete] A download job you started has finished. "
                    f"Job {jid} ({what} download); file saved to {out}{size_str}. "
                )
                if plan is not None:
                    return base + (
                        f"The user's plan is still active ({plan['remaining']} of {plan['total']} task(s) left). "
                        "Continue it immediately: if this result feeds the next plan step, do that step now "
                        "and update the plan tool as you go. Do not ask the user for permission to continue "
                        "and do not offer next steps. Report to the user only on a genuine blocker or when "
                        "the whole plan is complete. Do not mention this internal note."
                    )
                return base + (
                    "Reply to the user briefly and naturally: confirm the download is done, name the file. "
                    "If the user's request included further steps (e.g. summarize it), do them now without "
                    "asking. Only if the request is fully satisfied, offer one or two optional next steps "
                    "(e.g. transcribe the audio, ingest it into the knowledge library). Do not mention this "
                    "internal note."
                )
            if job.get("status") == "error":
                base = (
                    f"[gremlin-internal:download-failed] A download job you started FAILED. "
                    f"Job {jid} ({what} download); error: {job.get('error', 'unknown')}. "
                )
                if plan is not None:
                    return base + (
                        "The user's plan is still active. This failure may block a plan step: if a task "
                        "depends on this output, record the block with the plan tool, then report the "
                        "failure to the user with the reason. Do not mention this internal note."
                    )
                return base + (
                    "Reply to the user briefly: the download failed, say why, and offer to retry or "
                    "suggest a fix (check the URL, check the connection). Do not mention this internal note."
                )
            base = (
                f"[gremlin-internal:download-stopped] A download job you started was stopped. "
                f"Job {jid} ({what} download); no complete file was produced. "
            )
            if plan is not None:
                return base + (
                    "The user's plan is still active, but no complete file was produced: if a plan step "
                    "depends on it, record the block with the plan tool and report the stop to the user. "
                    "Do not mention this internal note."
                )
            return base + (
                "Reply to the user briefly: the download was stopped. Offer to restart it. "
                "Do not mention this internal note."
            )
        # transcribe
        dur = job.get("duration_sec") or 0.0
        model = job.get("model", "")
        if job.get("status") == "done":
            base = (
                f"[gremlin-internal:transcription-complete] A transcription job you started has finished. "
                f"Job {jid} (model {model}); transcript saved to {out} (source duration ~{dur:.0f}s). "
            )
            if plan is not None:
                return base + (
                    f"The user's plan is still active ({plan['remaining']} of {plan['total']} task(s) left). "
                    "Continue it immediately: if this result feeds the next plan step (e.g. summarize the "
                    "transcript), do that step now and update the plan tool as you go. Do not ask the user "
                    "for permission to continue and do not offer next steps. Report to the user only on a "
                    "genuine blocker or when the whole plan is complete. Do not mention this internal note."
                )
            return base + (
                "Reply to the user briefly and naturally: confirm it's done, name the transcript file. "
                "If the user's request included further steps (e.g. summarize it, pull out quotes), do "
                "them now without asking. Only if the request is fully satisfied, offer one or two "
                "optional next steps (e.g. save it elsewhere). Do not mention this internal note."
            )
        if job.get("status") == "error":
            base = (
                f"[gremlin-internal:transcription-failed] A transcription job you started FAILED. "
                f"Job {jid}; error: {job.get('error', 'unknown')}. "
            )
            if plan is not None:
                return base + (
                    "The user's plan is still active. This failure may block a plan step: if a task "
                    "depends on this transcript, record the block with the plan tool, then report the "
                    "failure to the user with the reason. Do not mention this internal note."
                )
            return base + (
                "Reply to the user briefly: the transcription failed, say why, and offer to retry or try a "
                "different model. Do not mention this internal note."
            )
        base = (
            f"[gremlin-internal:transcription-stopped] A transcription job you started was stopped. "
            f"Job {jid}; partial transcript saved at {out}. "
        )
        if plan is not None:
            return base + (
                "The user's plan is still active, but only a partial transcript was produced: if a plan "
                "step depends on the full transcript, record the block with the plan tool and report the "
                "stop to the user. Do not mention this internal note."
            )
        return base + (
            "Reply to the user briefly: the transcription was stopped, and the partial transcript is saved "
            "at that path. Offer next steps. Do not mention this internal note."
        )

    def _notify(self, kind: str, job: dict) -> None:
        job_id = job.get("id")
        session_id = job.get("session_id")
        try:
            self.sessions.get(session_id)
        except Exception:
            log.info("job %s (%s): session %s no longer exists; dropping", job_id, kind, session_id)
            for _kind, jobs_dir in self._sources():
                path = jobs_dir / f"{job_id}.json"
                if path.exists():
                    self._mark(jobs_dir, job_id, {"notified": True, "notified_reason": "session_gone"})
                    return
        if not self._wait_free(session_id):
            log.info("job %s (%s): deferring (session busy or monitor stopping)", job_id, kind)
            return
        try:
            for _ in self.manager.run(session_id, self._trigger(kind, job), self.load_settings()):
                pass
        except Exception:
            log.exception("job %s (%s): completion turn failed", job_id, kind)
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
                log.exception("job %s (%s): discord delivery failed", job_id, kind)
        for _kind, jobs_dir in self._sources():
            path = jobs_dir / f"{job_id}.json"
            if path.exists():
                self._mark(jobs_dir, job_id, {"notified": True})
                break
        log.info("job %s (%s): notified (status=%s, reply=%d chars)",
                 job_id, kind, job.get("status"), len(reply))
