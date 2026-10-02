"""JobMonitor: completion notification, dead-worker sweep, deferral."""

from __future__ import annotations

import json
import os
import time
import uuid

from sessions import SessionManager
from tools.job_monitor import DEAD_GRACE_SEC, JobMonitor


class FakeManager:
    """Minimal ChatManager stand-in: never busy; each run() appends an
    assistant message to the session and yields a done event."""

    def __init__(self, sessions, busy=False):
        self.sessions = sessions
        self.turns = []
        self._busy = busy

    def is_busy(self, session_id):
        return self._busy

    def run(self, session_id, user_message, settings):
        self.turns.append((session_id, user_message))
        self.sessions.add_message(session_id, {
            "id": uuid.uuid4().hex,
            "role": "assistant",
            "content": "The job finished.",
            "created_at": time.time(),
        })
        yield {"type": "done"}


def _make(cfg, sessions=None, busy=False, tmp=None):
    sessions = sessions or SessionManager(cfg)
    manager = FakeManager(sessions, busy=busy)
    monitor = JobMonitor(cfg, manager, sessions, lambda: {})
    return monitor, manager, sessions


def _state_path(cfg, kind, job_id):
    d = cfg.transcribe_jobs_dir if kind == "transcribe" else cfg.download_jobs_dir
    return d / f"{job_id}.json"


def _write_job(cfg, kind, job, **overrides):
    job.update(overrides)
    path = _state_path(cfg, kind, job["id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(job))
    return job


def _transcribe_job(session_id, **over):
    job = {
        "id": "t1",
        "session_id": session_id,
        "input_path": "/x/a.mp3",
        "output_path": "/x/a-transcription.txt",
        "model": "turbo",
        "language": "en",
        "status": "done",
        "started_at": time.time() - 60,
        "pid": None,
        "duration_sec": 12.0,
        "current_offset_sec": 12.0,
        "eta_sec": 0.0,
        "error": None,
        "finished_at": time.time(),
        "notified": False,
    }
    job.update(over)
    return job


def _download_job(session_id, **over):
    job = {
        "id": "d1",
        "kind": "audio",
        "session_id": session_id,
        "url": "https://youtu.be/x",
        "title": "Test Video",
        "channel": "tester",
        "output_path": "/x/2005-04-23_Test Video.mp3",
        "dest_dir": "/x",
        "stem": "2005-04-23_Test Video",
        "status": "done",
        "started_at": time.time() - 60,
        "pid": None,
        "progress_pct": 100.0,
        "error": None,
        "finished_at": time.time(),
        "notified": False,
    }
    job.update(over)
    return job


def test_transcribe_done_notified(cfg):
    monitor, manager, sessions = _make(cfg)
    sid = sessions.create("job test")["id"]
    _write_job(cfg, "transcribe", _transcribe_job(sid))

    notified = monitor._scan()

    assert [(k, j["id"]) for k, j in notified] == [("transcribe", "t1")]
    state = json.loads(_state_path(cfg, "transcribe", "t1").read_text())
    assert state["notified"] is True
    assert len(manager.turns) == 1
    trigger = manager.turns[0][1]
    assert "transcription" in trigger
    msgs = sessions.get(sid)["messages"]
    assert msgs and msgs[-1]["role"] == "assistant"


def test_download_done_notified_names_output(cfg):
    monitor, manager, sessions = _make(cfg)
    sid = sessions.create("dl test")["id"]
    _write_job(cfg, "download", _download_job(sid, size_bytes=305000))

    notified = monitor._scan()

    assert [(k, j["id"]) for k, j in notified] == [("download", "d1")]
    trigger = manager.turns[0][1]
    assert "/x/2005-04-23_Test Video.mp3" in trigger
    assert "download" in trigger
    state = json.loads(_state_path(cfg, "download", "d1").read_text())
    assert state["notified"] is True


def test_dead_worker_marked_error_and_notified(cfg):
    monitor, manager, sessions = _make(cfg)
    sid = sessions.create("dead")["id"]
    _write_job(cfg, "download", _download_job(sid, status="running",
                                              pid=99999999,
                                              started_at=time.time() - 3600,
                                              error=None))

    monitor._scan()

    state = json.loads(_state_path(cfg, "download", "d1").read_text())
    assert state["status"] == "error"
    assert "died" in state["error"]
    assert state["notified"] is True
    assert len(manager.turns) == 1
    assert "download" in manager.turns[0][1]


def test_live_worker_untouched(cfg):
    monitor, manager, sessions = _make(cfg)
    sid = sessions.create("live")["id"]
    _write_job(cfg, "download", _download_job(sid, status="running",
                                              pid=os.getpid(),
                                              started_at=time.time() - 3600))

    monitor._scan()

    state = json.loads(_state_path(cfg, "download", "d1").read_text())
    assert state["status"] == "running"
    assert state["notified"] is False
    assert manager.turns == []


def test_young_running_job_not_swept(cfg):
    monitor, manager, sessions = _make(cfg)
    sid = sessions.create("young")["id"]
    _write_job(cfg, "download", _download_job(sid, status="running",
                                              pid=99999999,
                                              started_at=time.time() - (DEAD_GRACE_SEC / 2)))

    monitor._scan()

    state = json.loads(_state_path(cfg, "download", "d1").read_text())
    assert state["status"] == "running"
    assert manager.turns == []


def test_session_gone_marks_notified_without_turn(cfg):
    monitor, manager, sessions = _make(cfg)
    _write_job(cfg, "download", _download_job("no-such-session"))

    monitor._scan()

    state = json.loads(_state_path(cfg, "download", "d1").read_text())
    assert state["notified"] is True
    assert state["notified_reason"] == "session_gone"
    assert manager.turns == []


def test_notified_jobs_not_reannounced(cfg):
    monitor, manager, sessions = _make(cfg)
    sid = sessions.create("once")["id"]
    _write_job(cfg, "download", _download_job(sid, notified=True))

    assert monitor._scan() == []
    assert manager.turns == []


def test_busy_session_defers_until_stop(cfg):
    monitor, manager, sessions = _make(cfg, busy=True)
    sid = sessions.create("busy")["id"]
    _write_job(cfg, "download", _download_job(sid))
    monitor._stop.set()  # makes _wait_free give up immediately

    monitor._scan()

    state = json.loads(_state_path(cfg, "download", "d1").read_text())
    assert state["notified"] is False
    assert manager.turns == []


def test_start_stop_lifecycle(cfg):
    monitor, *_ = _make(cfg)
    monitor.start()
    try:
        assert monitor.running
        monitor.stop()
    finally:
        monitor.stop()
    assert not monitor.running
