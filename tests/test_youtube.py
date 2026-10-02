"""YouTube tools: subtitle picking, VTT parsing, error handling.

Network calls are stubbed: ``_extract`` is monkeypatched per test and
``_download_subtitle`` returns canned VTT payloads.
"""

from __future__ import annotations

import pytest
from pathlib import Path
from tools import build_registry
from tools.sanitize import BANNER
from tools.youtube import (
    YoutubeError,
    _download_opts,
    _pick_subtitle,
    _parse_vtt,
    build_youtube_tools,
    download_audio,
    download_status,
    download_video,
    fetch_transcript,
    fetch_video_info,
    kill_download,
    run_download,
)


def _info(manual=None, auto=None, **extra):
    info = {
        "title": "Test Video",
        "uploader": "tester",
        "duration": 125,
        "view_count": 42,
        "description": "a video",
        "subtitles": manual or {},
        "automatic_captions": auto or {},
    }
    info.update(extra)
    return info


def _sub(ext="vtt", url="http://subs/example.vtt"):
    return [{"ext": ext, "url": url}]


# --- _pick_subtitle -------------------------------------------------------

def test_manual_preferred_over_auto():
    info = _info(manual={"en": _sub()}, auto={"en": _sub(url="http://subs/auto.vtt")})
    entry, source = _pick_subtitle(info)
    assert source == "manual"
    assert entry["url"].endswith("example.vtt")


def test_falls_back_to_auto():
    info = _info(auto={"en": _sub()})
    entry, source = _pick_subtitle(info)
    assert source == "auto"


def test_base_language_match():
    info = _info(auto={"en-US": _sub()})
    entry, source = _pick_subtitle(info)
    assert source == "auto"


def test_no_subtitles_lists_available():
    info = _info(manual={"de": _sub()}, auto={"fr": _sub()})
    with pytest.raises(YoutubeError) as ei:
        _pick_subtitle(info)
    assert "de" in str(ei.value) and "fr" in str(ei.value)


def test_no_subtitles_at_all():
    with pytest.raises(YoutubeError, match="no English subtitles"):
        _pick_subtitle(_info())


# --- _parse_vtt -----------------------------------------------------------

MANUAL_VTT = """WEBVTT
Kind: captions
Language: en

00:00:00.000 --> 00:00:02.000
Hello world.

00:00:02.000 --> 00:00:04.000
This is a test.
"""

AUTO_VTT = """WEBVTT
Kind: asr
Language: en

00:00:00.000 --> 00:00:01.000
 <c0.0>Hel</c>

00:00:01.000 --> 00:00:02.000
 <c0.0>Hel</c><c0.1>lo</c>

00:00:02.000 --> 00:00:04.000
 <c0.0>Hel</c><c0.1>lo</c> <c1.0>world</c>
"""


def test_parse_manual_vtt():
    assert _parse_vtt(MANUAL_VTT) == "Hello world.\nThis is a test."


def test_parse_auto_vtt_dedupes():
    out = _parse_vtt(AUTO_VTT)
    assert out == "Hello world"


def test_parse_strips_tags_and_blank():
    assert _parse_vtt("a <i>b</i>\n\n  ") == "a b"


# --- end-to-end with stubs --------------------------------------------------

@pytest.fixture
def stubbed(monkeypatch):
    import tools.youtube as y

    monkeypatch.setattr(y, "_extract", lambda url: _info(manual={"en": _sub()}, auto={}))
    monkeypatch.setattr(y, "_download_subtitle", lambda url: MANUAL_VTT)
    return y



def test_transcript_header_and_body(stubbed):
    out = fetch_transcript("https://youtu.be/x")
    assert out.startswith(BANNER)
    assert "Video: Test Video\nTranscript source: manual (en)" in out
    assert "Hello world." in out


def test_transcript_truncation(stubbed):
    out = fetch_transcript("https://youtu.be/x", limit=50)
    assert out.endswith("[truncated]")
    assert len(out) <= 50 + len("\n[truncated]") + len(BANNER) + 2


def test_video_info(stubbed):
    out = fetch_video_info("https://youtu.be/x")
    assert "Title: Test Video" in out
    assert "Duration: 2:05" in out
    assert "Views: 42" in out


def test_tool_descriptions_flag_untrusted(cfg):
    for t in build_youtube_tools(cfg):
        assert "untrusted external source" in t.description


def test_registry_error_strings(monkeypatch, cfg):
    import tools.youtube as y

    monkeypatch.setattr(
        y, "_extract",
        lambda url: (_ for _ in ()).throw(YoutubeError("bad url")),
    )
    reg = build_registry(cfg)
    assert "youtube_transcript" in reg.names()
    assert "youtube_video_info" in reg.names()
    out, ok = reg.execute("youtube_transcript", {"url": "not a url"})
    assert ok is False
    assert out.startswith("ERROR:")
    assert "bad url" in out


def test_registry_validation(cfg):
    reg = build_registry(cfg)
    out, ok = reg.execute("youtube_transcript", {})
    assert ok is False
    assert "url" in out
    out, ok = reg.execute("youtube_transcript", {"url": "x", "bogus": 1})
    assert ok is False
    assert "bogus" in out


def test_upload_date_format():
    from tools.youtube import _upload_date

    assert _upload_date({"upload_date": "20050423"}) == "2005-04-23"
    assert _upload_date({"upload_date": None}) == ""
    assert _upload_date({}) == ""
    assert _upload_date({"upload_date": "2005-04-23"}) == ""


def test_transcript_saved_to_files(stubbed, tmp_path, monkeypatch):
    from config import AppConfig

    monkeypatch.setattr(stubbed, "_extract", lambda url: _info(
        manual={"en": _sub()}, auto={}, upload_date="20050423"))
    cfg = AppConfig(root=tmp_path)
    out = fetch_transcript("https://youtu.be/x", cfg=cfg)
    dest = tmp_path / "files" / "transcripts" / "tester" / "2005-04-23_Test Video.txt"
    assert dest.is_file()
    assert dest.read_text(encoding="utf-8").startswith("Video: Test Video")
    assert "Uploaded: 2005-04-23" in dest.read_text(encoding="utf-8")
    assert f"Saved to: {dest.relative_to(tmp_path)}" in out


# --- downloads (background jobs) -------------------------------------------

@pytest.fixture
def fake_download(monkeypatch):
    """Patch ``yt_dlp.YoutubeDL`` so downloads write a fake media file,
    while the REAL constructor still runs on the exact opts the tool
    builds (a malformed ``postprocessors`` value raises in
    ``YoutubeDL.__init__`` before any download is attempted)."""
    import yt_dlp

    real_ytdl = yt_dlp.YoutubeDL
    ext = {"value": "mp4"}

    class FakeDL:
        def __init__(self, opts):
            self._outtmpl = opts.get("outtmpl")  # before real ctor normalizes it
            real_ytdl(opts)
            self._opts = opts

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False
        def extract_info(self, url, download=False):
            target = str(self._outtmpl).replace(".%(ext)s", "." + ext["value"])
            Path(target).write_bytes(b"fake media")
            return {}

    monkeypatch.setattr("yt_dlp.YoutubeDL", FakeDL)
    return ext


def test_run_download_audio_opts_accepted_by_ytdlp(stubbed, fake_download, tmp_path):
    """Regression: yt-dlp requires ``postprocessors`` as a list of dicts;
    the audio download opts must be accepted by the real constructor."""
    from config import AppConfig

    fake_download["value"] = "mp3"
    cfg = AppConfig(root=tmp_path)
    dest = cfg.audio_dir / "tester"
    result = run_download("https://youtu.be/x", dest, "2005-04-23_Test Video", _download_opts("audio"))
    assert result.is_file() and result.suffix == ".mp3"


def test_run_download_video_opts_accepted_by_ytdlp(stubbed, fake_download, tmp_path):
    from config import AppConfig

    cfg = AppConfig(root=tmp_path)
    dest = cfg.videos_dir / "tester"
    result = run_download("https://youtu.be/x", dest, "2005-04-23_Test Video", _download_opts("video"))
    assert result.is_file() and result.suffix == ".mp4"


class _FakeProc:
    pid = 4321


def _popen_recorder(monkeypatch, y):
    calls = []

    def fake_popen(argv, **kw):
        calls.append((argv, kw))
        return _FakeProc()

    monkeypatch.setattr(y.subprocess, "Popen", fake_popen)
    return calls


def test_download_audio_starts_job(stubbed, cfg, monkeypatch):
    import json

    y = stubbed
    calls = _popen_recorder(monkeypatch, y)
    monkeypatch.setattr(y, "_extract", lambda url: _info(manual={}, auto={}, upload_date="20050423"))

    out = download_audio("https://youtu.be/x", cfg)
    assert out.startswith(BANNER)
    data = json.loads(out[out.index("{"):])
    assert data["status"] == "running"
    assert data["kind"] == "audio"
    assert data["output_path"].endswith(".mp3")
    state = json.loads((cfg.download_jobs_dir / f"{data['job_id']}.json").read_text())
    assert state["pid"] == 4321
    assert state["notified"] is False
    assert state["output_path"] == data["output_path"]
    argv, kw = calls[0]
    assert argv[1:3] == ["-m", "tools.download_worker"]
    assert argv[3] == str(cfg.download_jobs_dir / f"{data['job_id']}.json")
    assert kw["cwd"] == str(cfg.root)


def test_download_video_starts_job(stubbed, cfg, monkeypatch):
    import json

    y = stubbed
    calls = _popen_recorder(monkeypatch, y)
    monkeypatch.setattr(y, "_extract", lambda url: _info(manual={}, auto={}, upload_date="20050423"))

    out = download_video("https://youtu.be/x", cfg)
    assert out.startswith(BANNER)
    data = json.loads(out[out.index("{"):])
    assert data["status"] == "running"
    assert data["kind"] == "video"
    assert data["output_path"].endswith(".mp4")
    state = json.loads((cfg.download_jobs_dir / f"{data['job_id']}.json").read_text())
    assert state["kind"] == "video"
    assert state["stem"] == "2005-04-23_Test Video"
    assert calls[0][0][1:3] == ["-m", "tools.download_worker"]


def _write_state(cfg, job_id, **overrides):
    import json
    import time

    state = {
        "id": job_id,
        "kind": "audio",
        "session_id": "sess-1",
        "url": "https://youtu.be/x",
        "title": "Test Video",
        "channel": "tester",
        "output_path": "/x/2005-04-23_Test Video.mp3",
        "dest_dir": "/x",
        "stem": "2005-04-23_Test Video",
        "status": "running",
        "started_at": time.time(),
        "pid": None,
        "progress_pct": 0.0,
        "error": None,
        "finished_at": None,
        "notified": False,
    }
    state.update(overrides)
    cfg.download_jobs_dir.mkdir(parents=True, exist_ok=True)
    (cfg.download_jobs_dir / f"{job_id}.json").write_text(json.dumps(state))
    return state


def test_download_status_running_and_terminal(cfg):
    import json

    _write_state(cfg, "j1", status="running", progress_pct=42.5)
    data = json.loads(download_status(cfg, "j1"))
    assert data["status"] == "running"
    assert "42%" in data["progress"]

    _write_state(cfg, "j1", status="done", error=None)
    data = json.loads(download_status(cfg, "j1"))
    assert data["status"] == "done"

    assert "unknown job id" in download_status(cfg, "nope")


def test_kill_download_dead_pid_marks_killed(cfg):
    import json

    _write_state(cfg, "j2", status="running", pid=99999999)
    data = json.loads(kill_download(cfg, "j2"))
    assert data["status"] == "killed"
    state = json.loads((cfg.download_jobs_dir / "j2.json").read_text())
    assert state["status"] == "killed"


def test_kill_download_not_running(cfg):
    import json

    _write_state(cfg, "j3", status="done", pid=99999999)
    data = json.loads(kill_download(cfg, "j3"))
    assert "not running" in data["note"]
