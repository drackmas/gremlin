"""TTS: markdown sanitizer, Piper engine (multi-voice), /api/tts endpoints,
settings persistence, and the frontend tts.js under Node."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from tts import (
    DEFAULT_VOICE,
    MAX_TEXT_CHARS,
    VOICES,
    PiperTTS,
    download_voice,
    sanitize_for_speech,
    voice_by_id,
    voice_url_parts,
)


def _write_voice(cfg, voice_id: str = DEFAULT_VOICE) -> None:
    """Create empty model files for *voice_id* under the temp piper dir."""
    v = voice_by_id(voice_id)
    (cfg.piper_dir / v.model_file).write_bytes(b"")
    (cfg.piper_dir / v.config_file).write_text(json.dumps({"audio": {"sample_rate": 22050}}))


# --- sanitizer --------------------------------------------------------------

def test_sanitize_bold_and_italic():
    assert sanitize_for_speech("**bold** and *em* and __under__") == "bold and em and under"
    # word-internal underscores are not emphasis
    assert sanitize_for_speech("use snake_case here") == "use snake_case here"


def test_sanitize_leftover_bold_markers():
    # Unmatched double-asterisks must not survive to speech
    assert sanitize_for_speech("a ** b") == "a b"
    assert sanitize_for_speech("**bold") == "bold"
    assert sanitize_for_speech("**ok** and **left") == "ok and left"


def test_sanitize_inline_code():
    assert sanitize_for_speech("run `pip install x` now") == "run pip install x now"
    assert sanitize_for_speech("stray ` backtick") == "stray backtick"


def test_sanitize_links_images_urls():
    assert sanitize_for_speech("see [the docs](https://example.com/a) ok") == "see the docs ok"
    assert sanitize_for_speech("![alt text](https://x/y.png)") == "alt text"
    assert sanitize_for_speech("raw https://example.com/x?q=1 here") == "raw link here"


def test_sanitize_fenced_code():
    assert (
        sanitize_for_speech("Intro:\n```python\nprint(1)\n```\nDone.")
        == "Intro: Code block. Done."
    )
    # fence with no closer (block split across streamed chunks): body dropped
    assert sanitize_for_speech("Intro\n```\ncode line") == "Intro Code block."


def test_sanitize_headings_lists_blockquote_hrule():
    out = sanitize_for_speech("# Title\n> quoted\n- one\n- two\n1. three\n---\nafter")
    assert out == "Title quoted one two three after"


def test_sanitize_html_tags():
    assert sanitize_for_speech("<b>bold</b> and <script>x</script>end") == "bold and end"


def test_sanitize_keeps_prose_punctuation():
    text = "It's 42. Really? Yes, sure! (Parentheses) — dashes."
    assert sanitize_for_speech(text) == text


def test_sanitize_collapses_whitespace():
    assert sanitize_for_speech("a\n\n   b\t\tc") == "a b c"


def test_sanitize_pure_syntax_yields_nothing():
    assert sanitize_for_speech("---") == ""
    assert sanitize_for_speech("***\n***") == ""


# --- voice registry -----------------------------------------------------------

def test_voices_registry():
    ids = [v.id for v in VOICES]
    assert len(set(ids)) == len(ids)
    assert DEFAULT_VOICE in ids
    v = voice_by_id("en_US-amy-medium")
    assert v.model_file == "en_US-amy-medium.onnx"
    assert v.config_file == "en_US-amy-medium.onnx.json"
    with pytest.raises(ValueError, match="unknown voice"):
        voice_by_id("en_NO-nobody-medium")


# --- engine module ------------------------------------------------------------

def test_synthesize_rejects_empty(cfg):
    t = PiperTTS(cfg.piper_dir)
    with pytest.raises(ValueError, match="empty"):
        list(t.synthesize_chunks("   "))


def test_synthesize_rejects_overlong(cfg):
    t = PiperTTS(cfg.piper_dir)
    with pytest.raises(ValueError, match="at most"):
        list(t.synthesize_chunks("x" * (MAX_TEXT_CHARS + 1)))


class _FakeChunk:
    def __init__(self, audio_int16_bytes: bytes):
        self.audio_int16_bytes = audio_int16_bytes


class _FakeVoice:
    def __init__(self):
        self.synthesized: list[str] = []

    def synthesize(self, text: str):
        self.synthesized.append(text)
        return [_FakeChunk(b"\x11\x22" * 10000)]


def test_synthesize_chunks_size_and_content(cfg, monkeypatch):
    t = PiperTTS(cfg.piper_dir)
    fake = _FakeVoice()
    monkeypatch.setattr(t, "_ensure_voice", lambda voice_id: fake)
    chunks = list(t.synthesize_chunks("hello"))
    # 20000 bytes re-sliced to CHUNK_SIZE (8192) => 8192 + 8192 + 3616
    assert [len(c) for c in chunks] == [8192, 8192, 3616]
    assert b"".join(chunks) == b"\x11\x22" * 10000


def test_engine_passes_sanitized_text_to_model(cfg, monkeypatch):
    t = PiperTTS(cfg.piper_dir)
    fake = _FakeVoice()
    monkeypatch.setattr(t, "_ensure_voice", lambda voice_id: fake)
    list(t.synthesize_chunks("**bold** and [the docs](https://x.y) now", voice_id="en_US-amy-medium"))
    assert fake.synthesized == ["bold and the docs now"]


def test_synthesize_empty_after_sanitize_yields_nothing(cfg):
    """Pure syntax (nothing to speak) must not load a voice or yield PCM."""
    t = PiperTTS(cfg.piper_dir)  # no model files exist in the temp tree
    assert list(t.synthesize_chunks("---")) == []
    assert t._loaded == {}


def test_model_available_false_and_describe_missing(cfg):
    t = PiperTTS(cfg.piper_dir)
    assert t.available() is False
    assert "en_GB-alba-medium.onnx" in t.describe_missing()


def test_model_available_true_when_files_exist(cfg):
    _write_voice(cfg)
    t = PiperTTS(cfg.piper_dir)
    assert t.available() is True
    assert t.available("en_US-amy-medium") is False
    assert t.describe_missing() == ""
    assert "en_US-amy-medium.onnx" in t.describe_missing("en_US-amy-medium")


def test_sample_rate_from_config(cfg):
    _write_voice(cfg)
    t = PiperTTS(cfg.piper_dir)
    assert t.sample_rate() == 22050


def test_per_voice_caches_are_independent(cfg, monkeypatch):
    t = PiperTTS(cfg.piper_dir)
    a, b = _FakeVoice(), _FakeVoice()
    monkeypatch.setattr(
        t, "_ensure_voice", lambda voice_id: a if voice_id == DEFAULT_VOICE else b
    )
    list(t.synthesize_chunks("one"))
    list(t.synthesize_chunks("two", voice_id="en_US-amy-medium"))
    assert a.synthesized == ["one"]
    assert b.synthesized == ["two"]


# --- /api/tts endpoints ---------------------------------------------------------

def test_tts_voices_endpoint(client):
    resp = client.get("/api/tts/voices")
    assert resp.status_code == 200
    voices = resp.get_json()
    assert [v["id"] for v in voices] == [v.id for v in VOICES]
    # tmp tree has no model files: nothing available yet
    assert all(v["available"] is False for v in voices)


def test_tts_voices_endpoint_marks_available(client, cfg):
    _write_voice(cfg)
    by_id = {v["id"]: v for v in client.get("/api/tts/voices").get_json()}
    assert by_id["en_GB-alba-medium"]["available"] is True
    assert by_id["en_US-amy-medium"]["available"] is False


def test_tts_disabled_returns_400(client):
    resp = client.post("/api/tts/stream", json={"text": "hello"})
    assert resp.status_code == 400
    assert "disabled" in resp.get_json()["error"]


def test_tts_empty_text_returns_400(client):
    client.post("/api/settings", json={"piper_tts_enabled": True})
    resp = client.post("/api/tts/stream", json={"text": "   "})
    assert resp.status_code == 400
    assert "empty" in resp.get_json()["error"]


def test_tts_overlong_returns_400(client):
    client.post("/api/settings", json={"piper_tts_enabled": True})
    resp = client.post("/api/tts/stream", json={"text": "x" * (MAX_TEXT_CHARS + 1)})
    assert resp.status_code == 400
    assert "at most" in resp.get_json()["error"]


def test_tts_model_missing_returns_503(client):
    client.post("/api/settings", json={"piper_tts_enabled": True})
    # tmp config has no model files under models/piper/
    resp = client.post("/api/tts/stream", json={"text": "hello"})
    assert resp.status_code == 503
    assert "not found" in resp.get_json()["error"]


def test_tts_stream_ok_headers_and_pcm(client, monkeypatch):
    client.post("/api/settings", json={"piper_tts_enabled": True})
    # Fake the voice so no real ONNX model is needed.
    monkeypatch.setattr(PiperTTS, "available", lambda self, voice_id=DEFAULT_VOICE: True)
    monkeypatch.setattr(PiperTTS, "ensure_ready", lambda self, voice_id=DEFAULT_VOICE: 22050)

    def fake_chunks(self, text, voice_id=DEFAULT_VOICE):
        yield b"\x01\x02\x03\x04" * 4096  # 16384 bytes of raw PCM

    monkeypatch.setattr(PiperTTS, "synthesize_chunks", fake_chunks)

    resp = client.post("/api/tts/stream", json={"text": "Hello there."})
    assert resp.status_code == 200
    assert resp.content_type == "application/octet-stream"
    assert resp.headers["X-Audio-Sample-Rate"] == "22050"
    assert resp.headers["X-Audio-Format"] == "s16le"
    assert resp.headers["X-Audio-Channels"] == "1"
    assert resp.headers["Cache-Control"] == "no-cache"
    # no WAV header — pure PCM
    assert resp.data == b"\x01\x02\x03\x04" * 4096


def test_tts_stream_uses_selected_voice(client, monkeypatch):
    client.post(
        "/api/settings",
        json={"piper_tts_enabled": True, "piper_voice": "en_US-amy-medium"},
    )
    loaded: list[str] = []
    monkeypatch.setattr(PiperTTS, "available", lambda self, voice_id=DEFAULT_VOICE: True)
    monkeypatch.setattr(
        PiperTTS, "ensure_ready", lambda self, voice_id=DEFAULT_VOICE: loaded.append(voice_id) or 22050
    )

    def fake_chunks(self, text, voice_id=DEFAULT_VOICE):
        loaded.append(voice_id)
        yield b"\x01\x02\x03\x04"

    monkeypatch.setattr(PiperTTS, "synthesize_chunks", fake_chunks)

    resp = client.post("/api/tts/stream", json={"text": "Hi."})
    assert resp.status_code == 200
    assert loaded == ["en_US-amy-medium", "en_US-amy-medium"]


def test_tts_stream_sanitizes_markdown(client, monkeypatch):
    """Markdown is rewritten before synthesis: Piper never sees emphasis markers."""
    client.post("/api/settings", json={"piper_tts_enabled": True})
    fake = _FakeVoice()
    monkeypatch.setattr(PiperTTS, "available", lambda self, voice_id=DEFAULT_VOICE: True)
    monkeypatch.setattr(PiperTTS, "ensure_ready", lambda self, voice_id=DEFAULT_VOICE: 22050)
    monkeypatch.setattr(PiperTTS, "_ensure_voice", lambda self, voice_id: fake)

    resp = client.post("/api/tts/stream", json={"text": "Hi **bold**, a ** b, `code`"})
    assert resp.status_code == 200
    assert fake.synthesized == ["Hi bold, a b, code"]


def test_tts_stream_unknown_voice_returns_400(client, cfg):
    """Bypass settings validation (simulate a stale saved value)."""
    client.post("/api/settings", json={"piper_tts_enabled": True})
    data = json.loads(cfg.settings_path.read_text())
    data["piper_voice"] = "en_NO-nobody-medium"
    cfg.settings_path.write_text(json.dumps(data))
    resp = client.post("/api/tts/stream", json={"text": "hi"})
    assert resp.status_code == 400
    assert "unknown voice" in resp.get_json()["error"]


# --- settings ------------------------------------------------------------------

def test_piper_tts_setting_round_trip(client, cfg):
    data = client.get("/api/settings").get_json()
    assert data["piper_tts_enabled"] is False

    res = client.post("/api/settings", json={"piper_tts_enabled": True})
    assert res.status_code == 200
    assert res.get_json()["piper_tts_enabled"] is True
    # persisted to disk, not just echoed
    on_disk = json.loads(cfg.settings_path.read_text())
    assert on_disk["piper_tts_enabled"] is True


def test_piper_tts_setting_rejects_non_bool(client):
    for bad in ("yes", 1, 0, None):
        res = client.post("/api/settings", json={"piper_tts_enabled": bad})
        assert res.status_code == 400, bad


def test_piper_voice_setting_round_trip(client, cfg):
    res = client.post("/api/settings", json={"piper_voice": "en_US-lessac-medium"})
    assert res.status_code == 200
    assert res.get_json()["piper_voice"] == "en_US-lessac-medium"
    on_disk = json.loads(cfg.settings_path.read_text())
    assert on_disk["piper_voice"] == "en_US-lessac-medium"


def test_piper_voice_setting_rejects_unknown(client):
    res = client.post("/api/settings", json={"piper_voice": "en_NO-nobody-medium"})
    assert res.status_code == 400
    assert "piper_voice" in res.get_json()["error"]


# --- frontend sentence-splitting (real tts.js, via Node) ------------------

# Appended to the real tts.js source; runs in the same file scope so it can see
# the top-level `tts` binding. A fake `fetch` captures the text that would be
# synthesized; an empty PCM body keeps the Web Audio path from scheduling.
_NODE_HARNESS = r"""
;(async () => {
  const captured = [];
  class FakeCtx {
    constructor() { this.state = "running"; }
    resume() { return Promise.resolve(); }
  }
  global.window = { AudioContext: FakeCtx };
  const realFetch = async (url, opts) => {
    captured.push(JSON.parse(opts.body).text);
    return {
      ok: true, status: 200,
      headers: { get: (k) => (k === "X-Audio-Sample-Rate" ? "22050" : null) },
      arrayBuffer: async () => new ArrayBuffer(0),
    };
  };
  global.fetch = realFetch;
  const tick = (ms = 50) => new Promise((r) => setTimeout(r, ms));

  // Mid-stream: two complete sentences speak, trailing one waits for flush().
  tts.start();
  tts.push("Hello world. This is a test. How are you?");
  await tick();
  if (JSON.stringify(captured) !== JSON.stringify(["Hello world.", "This is a test."])) {
    console.error("FAIL mid-stream:", JSON.stringify(captured)); process.exit(1);
  }
  tts.flush();
  await tick();
  if (JSON.stringify(captured) !== JSON.stringify(["Hello world.", "This is a test.", "How are you?"])) {
    console.error("FAIL flush:", JSON.stringify(captured)); process.exit(1);
  }

  // A long run without punctuation is chunked to <= 2000 chars per request.
  captured.length = 0;
  tts.start();
  tts.push("A".repeat(5000));
  tts.flush();
  await tick();
  const lens = captured.map((s) => s.length);
  if (JSON.stringify(lens) !== JSON.stringify([2000, 2000, 1000])) {
    console.error("FAIL chunk lengths:", JSON.stringify(lens)); process.exit(1);
  }

  // stop() aborts a synthesis request that is still in flight, and start()
  // after stop() speaks again.
  captured.length = 0;
  let lateSignal = null;
  global.fetch = (url, opts) => new Promise((resolve, reject) => {
    lateSignal = opts.signal;
    // never resolves on its own; rejects when the caller aborts, like real fetch
    opts.signal.addEventListener("abort", () => {
      const e = new Error("aborted");
      e.name = "AbortError";
      reject(e);
    });
  });
  tts.start();
  tts.push("Interrupt me now. ");
  await tick(10);           // fetch is in flight
  if (!lateSignal) {
    console.error("FAIL no fetch signal seen"); process.exit(1);
  }
  tts.stop();
  if (lateSignal.aborted !== true) {
    console.error("FAIL stop did not abort in-flight fetch"); process.exit(1);
  }
  await tick(60);           // let the aborted fetch settle; nothing may schedule

  global.fetch = realFetch;
  tts.start();
  tts.push("Back on track.");
  tts.flush();
  await tick();
  if (JSON.stringify(captured) !== JSON.stringify(["Back on track."])) {
    console.error("FAIL restart after stop:", JSON.stringify(captured)); process.exit(1);
  }
  console.log("OK");
})();
"""


def test_frontend_sentence_splitting(tmp_path):
    """Run the real tts.js under Node; verify split/flush/chunking via a fake fetch."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    tts_js = Path(__file__).resolve().parent.parent / "frontend" / "static" / "js" / "tts.js"
    combined = tts_js.read_text() + "\n" + _NODE_HARNESS
    script = tmp_path / "tts_test.js"
    script.write_text(combined)
    proc = subprocess.run(
        [node, str(script)], capture_output=True, text=True, timeout=30
    )
    assert proc.returncode == 0, f"stdout={proc.stdout}\nstderr={proc.stderr}"
    assert "OK" in proc.stdout


# --- voice download helper ------------------------------------------------------


class _FakeResp:
    def __init__(self, data: bytes):
        self._data = data

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._data


def test_voice_url_parts():
    assert voice_url_parts("en_GB-alba-medium") == ("en", "en_GB", "alba", "medium")
    assert voice_url_parts("en_US-amy-medium") == ("en", "en_US", "amy", "medium")
    with pytest.raises(ValueError):
        voice_url_parts("xx_YY-nobody-medium")


def test_download_voice_creates_both_files(tmp_path):
    calls: list[str] = []

    def opener(url):
        calls.append(url)
        return _FakeResp(b"weights")

    out = download_voice("en_US-amy-medium", tmp_path, opener=opener)
    assert out == {"en_US-amy-medium.onnx": "ok", "en_US-amy-medium.onnx.json": "ok"}
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "en_US-amy-medium.onnx",
        "en_US-amy-medium.onnx.json",
    ]
    base = "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/en/en_US/amy/medium"
    assert calls == [f"{base}/en_US-amy-medium.onnx", f"{base}/en_US-amy-medium.onnx.json"]


def test_download_voice_skips_existing_files(tmp_path):
    (tmp_path / "en_US-amy-medium.onnx").write_bytes(b"existing")
    calls: list[str] = []

    def opener(url):
        calls.append(url)
        return _FakeResp(b"new")

    out = download_voice("en_US-amy-medium", tmp_path, opener=opener)
    assert out == {"en_US-amy-medium.onnx": "skipped", "en_US-amy-medium.onnx.json": "ok"}
    assert len(calls) == 1
    assert calls[0].endswith("en_US-amy-medium.onnx.json")
    # existing file is left untouched
    assert (tmp_path / "en_US-amy-medium.onnx").read_bytes() == b"existing"


def test_download_voice_unknown_id_raises(tmp_path):
    with pytest.raises(ValueError):
        download_voice("xx_YY-nobody-medium", tmp_path, opener=lambda url: _FakeResp(b""))


def test_download_voice_writes_bytes_and_creates_dir(tmp_path):
    dest = tmp_path / "nested" / "piper"
    out = download_voice("en_GB-alba-medium", dest, opener=lambda url: _FakeResp(b"\x00\x01"))
    assert out["en_GB-alba-medium.onnx"] == "ok"
    assert (dest / "en_GB-alba-medium.onnx").read_bytes() == b"\x00\x01"
