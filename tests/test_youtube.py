"""YouTube tools: subtitle picking, VTT parsing, error handling.

Network calls are stubbed: ``_extract`` is monkeypatched per test and
``requests.get`` returns canned VTT payloads.
"""

from __future__ import annotations

import pytest

from tools import build_registry
from tools.sanitize import BANNER
from tools.youtube import (
    YoutubeError,
    _pick_subtitle,
    _parse_vtt,
    build_youtube_tools,
    fetch_transcript,
    fetch_video_info,
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
    entry, source = _pick_subtitle(info, "en")
    assert source == "manual"
    assert entry["url"].endswith("example.vtt")


def test_falls_back_to_auto():
    info = _info(auto={"en": _sub()})
    entry, source = _pick_subtitle(info, "en")
    assert source == "auto"


def test_base_language_match():
    info = _info(auto={"en-US": _sub()})
    entry, source = _pick_subtitle(info, "en")
    assert source == "auto"


def test_no_subtitles_lists_available():
    info = _info(manual={"de": _sub()}, auto={"fr": _sub()})
    with pytest.raises(YoutubeError) as ei:
        _pick_subtitle(info, "ja")
    assert "de" in str(ei.value) and "fr" in str(ei.value)


def test_no_subtitles_at_all():
    with pytest.raises(YoutubeError, match="no subtitles"):
        _pick_subtitle(_info(), "en")


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

class _FakeResp:
    text = MANUAL_VTT
    def raise_for_status(self):
        pass


@pytest.fixture
def stubbed(monkeypatch):
    import tools.youtube as y

    monkeypatch.setattr(y, "_extract", lambda url: _info(manual={"en": _sub()}, auto={}))
    monkeypatch.setattr(y.requests, "get", lambda *a, **k: _FakeResp())
    return y


def test_transcript_header_and_body(stubbed):
    out = fetch_transcript("https://youtu.be/x", "en")
    assert out.startswith(BANNER)
    assert "Video: Test Video\nTranscript source: manual (en)" in out
    assert "Hello world." in out


def test_transcript_truncation(stubbed):
    out = fetch_transcript("https://youtu.be/x", "en", limit=50)
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
    out = fetch_transcript("https://youtu.be/x", "en", cfg=cfg)
    dest = tmp_path / "files" / "transcripts" / "tester" / "2005-04-23_Test Video.txt"
    assert dest.is_file()
    assert dest.read_text(encoding="utf-8").startswith("Video: Test Video")
    assert "Uploaded: 2005-04-23" in dest.read_text(encoding="utf-8")
    assert f"Saved to: {dest.relative_to(tmp_path)}" in out
