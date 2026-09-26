"""STT: settings validation, dual engine (faster-whisper + Parakeet via
onnx-asr; lazy load, decode, lock), /api/stt endpoints, barge-in
interruption, downloads, and the frontend stt.js threshold/silence logic
under Node."""

from __future__ import annotations

import io
import shutil
import subprocess
import wave
from pathlib import Path

import numpy as np
import onnx_asr
import pytest

from models.base import ModelBackend, ModelEvent
from stt import (
    DEFAULT_STT_MODEL,
    MAX_AUDIO_S,
    MIN_AUDIO_MS,
    MODELS,
    STT_MODEL_IDS,
    SAMPLE_RATE,
    STTEngine,
    STTModelError,
    download_model,
    model_by_id,
    model_urls,
)
from stt.engine import _decode_audio


#: A parakeet catalog id, for tests that monkeypatch the onnx-asr runtime.
PARAKEET_ID = "nemo-parakeet-tdt-0.6b-v2"


# --- helpers -----------------------------------------------------------------


def _write_model(cfg, model_id: str = DEFAULT_STT_MODEL) -> None:
    """Create (empty) model files for *model_id* under the temp stt dir."""
    m = model_by_id(model_id)
    base = cfg.stt_dir / m.id
    for f in m.files:
        target = base / f
        target.parent.mkdir(parents=True, exist_ok=True)
        if f.endswith(".json"):
            target.write_text("{}")
        else:
            target.write_bytes(b"")


def _sine(dur_s: float, rate: int = SAMPLE_RATE, amp: float = 0.5, freq: float = 440.0) -> np.ndarray:
    t = np.arange(int(dur_s * rate)) / rate
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def _wav(samples: np.ndarray, rate: int = SAMPLE_RATE) -> bytes:
    pcm = (np.clip(samples, -1, 1) * 32767).astype("<i2").tobytes()
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


def _wav_stereo(samples: np.ndarray, rate: int = SAMPLE_RATE) -> bytes:
    """Two identical channels (left = right) so the downmix is predictable."""
    pcm_l = (np.clip(samples, -1, 1) * 32767).astype("<i2")
    interleaved = np.empty(pcm_l.size * 2, dtype="<i2")
    interleaved[0::2] = pcm_l
    interleaved[1::2] = pcm_l
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(interleaved.tobytes())
    return buf.getvalue()


def _wav_8bit(samples: np.ndarray, rate: int = SAMPLE_RATE) -> bytes:
    pcm = ((np.clip(samples, -1, 1) + 1) / 2 * 255).astype("u1")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(1)
        w.setframerate(rate)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


class _FakeAdapter:
    """Stands in for TextResultsAsrAdapter: records recognize() calls."""

    def __init__(self, text: str = "hello world"):
        self.text = text
        self.calls: list[tuple] = []

    def recognize(self, waveform, sample_rate: int = SAMPLE_RATE, **kwargs) -> str:
        self.calls.append((waveform, sample_rate))
        return self.text


class _FakeWhisperAdapter:
    """Stands in for faster_whisper.WhisperModel: records transcribe() calls."""

    def __init__(self):
        self.calls: list[tuple] = []

    def transcribe(self, waveform, **kwargs):
        self.calls.append((waveform, kwargs))

        class _Seg:
            def __init__(self, t):
                self.text = t

        return ([_Seg(" hi "), _Seg(" there")]), None


# --- catalog -------------------------------------------------------------------


def test_catalog_shape():
    assert len(MODELS) == 6
    assert STT_MODEL_IDS == tuple(m.id for m in MODELS)
    assert DEFAULT_STT_MODEL in STT_MODEL_IDS
    assert model_by_id(DEFAULT_STT_MODEL).engine == "whisper"
    assert set(model_by_id(i).engine for i in STT_MODEL_IDS) == {"whisper", "parakeet"}
    for m in MODELS:
        assert m.approx_mb > 0
        assert m.files
        assert m.id in m.label or m.approx_mb
    with pytest.raises(ValueError, match="unknown STT model"):
        model_by_id("whisper-huge")


def test_engine_availability_and_describe_missing(cfg):
    e = STTEngine(cfg.stt_dir)
    assert e.available(DEFAULT_STT_MODEL) is False
    missing = e.describe_missing(DEFAULT_STT_MODEL)
    assert "vocabulary.txt" in missing and "model.bin" in missing
    _write_model(cfg)
    assert e.available(DEFAULT_STT_MODEL) is True
    assert e.describe_missing(DEFAULT_STT_MODEL) == ""


def test_engine_lazy_load(cfg, monkeypatch):
    _write_model(cfg, PARAKEET_ID)
    e = STTEngine(cfg.stt_dir)
    calls = []
    monkeypatch.setattr(onnx_asr, "load_model", lambda *a, **k: calls.append(1) or _FakeAdapter())
    e.available(PARAKEET_ID)  # availability must not load
    assert calls == [] and e._loaded == {}
    e.ensure_ready(PARAKEET_ID)
    assert len(calls) == 1 and len(e._loaded) == 1
    e.ensure_ready(PARAKEET_ID)  # cached: no second load
    assert len(calls) == 1


def test_engine_missing_model_raises(cfg):
    e = STTEngine(cfg.stt_dir)
    with pytest.raises(STTModelError, match="STT model files missing"):
        e.ensure_ready()
    with pytest.raises(STTModelError, match="python -m stt.download"):
        e.transcribe(_wav(_sine(0.5)))


# --- audio decode + transcribe ----------------------------------------------------


def test_transcribe_empty_raises(cfg):
    _write_model(cfg)
    with pytest.raises(ValueError, match="must not be empty"):
        STTEngine(cfg.stt_dir).transcribe(b"")


def test_transcribe_too_short_raises(cfg):
    _write_model(cfg)
    with pytest.raises(ValueError, match=f"at least {MIN_AUDIO_MS} ms"):
        STTEngine(cfg.stt_dir).transcribe(_wav(_sine(MIN_AUDIO_MS / 2000)))


def test_transcribe_rejects_overlong(cfg):
    _write_model(cfg)
    with pytest.raises(ValueError, match=f"max {MAX_AUDIO_S * 1000} ms"):
        STTEngine(cfg.stt_dir).transcribe(_wav(_sine(MAX_AUDIO_S + 1)))


def test_transcribe_raw_odd_bytes_raise_cleanly(cfg):
    with pytest.raises(ValueError, match="16-bit PCM"):
        _decode_audio(b"\x01\x02\x03")


def test_engine_transcribe_happy_path(cfg, monkeypatch):
    _write_model(cfg, PARAKEET_ID)
    adapter = _FakeAdapter("hello world")
    monkeypatch.setattr(onnx_asr, "load_model", lambda *a, **k: adapter)
    e = STTEngine(cfg.stt_dir)
    text = e.transcribe(_wav(_sine(1.0)), model_id=PARAKEET_ID)
    assert text == "hello world"
    waveform, rate = adapter.calls[0]
    assert waveform.dtype == np.float32
    assert waveform.size == SAMPLE_RATE  # 1 s at 16 kHz
    assert rate == SAMPLE_RATE
    assert abs(float(waveform.max())) < 1.0  # normalized to [-1, 1]


def test_engine_transcribe_whisper_path(cfg, monkeypatch):
    """Whisper models go through faster-whisper, not onnx-asr."""
    _write_model(cfg, "base")
    adapter = _FakeWhisperAdapter()
    monkeypatch.setattr("faster_whisper.WhisperModel", lambda *a, **k: adapter)
    e = STTEngine(cfg.stt_dir)
    assert e.transcribe(_wav(_sine(1.0)), model_id="base") == "hi there"
    waveform, _ = adapter.calls[0]
    assert waveform.dtype == np.float32
    assert waveform.size == SAMPLE_RATE


def test_transcribe_lock_held_during_inference(cfg, monkeypatch):
    """The ONNX session is not thread-safe: recognize() must run under the
    engine lock."""
    _write_model(cfg, PARAKEET_ID)
    e = STTEngine(cfg.stt_dir)
    seen: list[bool] = []

    class _LockSpy(_FakeAdapter):
        def recognize(self, waveform, sample_rate=SAMPLE_RATE, **kwargs):
            seen.append(e._lock.locked())
            return "x"

    monkeypatch.setattr(onnx_asr, "load_model", lambda *a, **k: _LockSpy())
    e.transcribe(_wav(_sine(0.5)), model_id=PARAKEET_ID)
    assert seen == [True]


def test_decode_raw_s16le(cfg, monkeypatch):
    """Bytes without a WAV header are treated as raw s16le at 16 kHz."""
    _write_model(cfg, PARAKEET_ID)
    adapter = _FakeAdapter()
    monkeypatch.setattr(onnx_asr, "load_model", lambda *a, **k: adapter)
    raw = _sine(0.5).astype("<i2").tobytes()
    assert STTEngine(cfg.stt_dir).transcribe(raw, model_id=PARAKEET_ID) == "hello world"
    assert adapter.calls[0][0].size == 0.5 * SAMPLE_RATE


def test_decode_multichannel_downmix(cfg, monkeypatch):
    _write_model(cfg, PARAKEET_ID)
    adapter = _FakeAdapter()
    monkeypatch.setattr(onnx_asr, "load_model", lambda *a, **k: adapter)
    STTEngine(cfg.stt_dir).transcribe(_wav_stereo(_sine(0.5)), model_id=PARAKEET_ID)
    waveform, _ = adapter.calls[0]
    assert waveform.size == 0.5 * SAMPLE_RATE  # one channel, not two


def test_decode_resamples_to_16k(cfg, monkeypatch):
    _write_model(cfg, PARAKEET_ID)
    adapter = _FakeAdapter()
    monkeypatch.setattr(onnx_asr, "load_model", lambda *a, **k: adapter)
    STTEngine(cfg.stt_dir).transcribe(_wav(_sine(1.0, rate=8000), rate=8000), model_id=PARAKEET_ID)
    waveform, rate = adapter.calls[0]
    assert waveform.size == SAMPLE_RATE
    assert rate == SAMPLE_RATE


def test_decode_8bit_wav(cfg, monkeypatch):
    _write_model(cfg, PARAKEET_ID)
    adapter = _FakeAdapter()
    monkeypatch.setattr(onnx_asr, "load_model", lambda *a, **k: adapter)
    STTEngine(cfg.stt_dir).transcribe(_wav_8bit(_sine(0.5)), model_id=PARAKEET_ID)
    assert adapter.calls[0][0].size == 0.5 * SAMPLE_RATE


def test_decode_rejects_unsupported_rate():
    with pytest.raises(ValueError, match="unsupported sample rate"):
        _decode_audio(_wav(_sine(0.5, rate=7000), rate=7000))


# --- /api/stt/models ---------------------------------------------------------------


def test_models_endpoint_lists_catalog(client, cfg):
    res = client.get("/api/stt/models")
    assert res.status_code == 200
    data = res.get_json()
    assert [m["id"] for m in data] == list(STT_MODEL_IDS)
    for m in data:
        assert set(m) == {"id", "engine", "label", "approx_mb", "available"}
        assert m["available"] is False  # fresh temp tree has no files
    _write_model(cfg)
    data = client.get("/api/stt/models").get_json()
    by_id = {m["id"]: m for m in data}
    assert by_id[DEFAULT_STT_MODEL]["available"] is True


# --- /api/stt/transcribe ---------------------------------------------------------------


def _enable_stt(client, **extra):
    res = client.post("/api/settings", json={"stt_enabled": True, **extra})
    assert res.status_code == 200


def test_transcribe_400_when_disabled(client):
    res = client.post("/api/stt/transcribe", json={"audio": "AQID"})
    assert res.status_code == 400
    assert "disabled" in res.get_json()["error"]


def test_transcribe_400_unknown_model(client):
    _enable_stt(client)
    res = client.post("/api/stt/transcribe", json={"audio": "AQID", "model": "whisper-huge"})
    assert res.status_code == 400
    assert "whisper-huge" in res.get_json()["error"]


@pytest.mark.parametrize("body", [{}, {"audio": ""}, {"audio": 123}])
def test_transcribe_400_missing_audio(client, body):
    _enable_stt(client)
    res = client.post("/api/stt/transcribe", json=body)
    assert res.status_code == 400
    assert "audio" in res.get_json()["error"]


def test_transcribe_400_bad_base64(client):
    _enable_stt(client)
    res = client.post("/api/stt/transcribe", json={"audio": "abc"})
    assert res.status_code == 400
    assert "base64" in res.get_json()["error"]


def test_transcribe_503_missing_files(client, cfg):
    _enable_stt(client)
    res = client.post("/api/stt/transcribe", json={"audio": "AQID"})
    assert res.status_code == 503
    err = res.get_json()["error"]
    assert "STT model not found" in err
    assert "model.bin" in err


def test_transcribe_400_audio_too_short(client, cfg, monkeypatch):
    _enable_stt(client)
    _write_model(cfg)
    monkeypatch.setattr(onnx_asr, "load_model", lambda *a, **k: _FakeAdapter())
    import base64

    audio = base64.b64encode(_wav(_sine(MIN_AUDIO_MS / 2000))).decode()
    res = client.post("/api/stt/transcribe", json={"audio": audio})
    assert res.status_code == 400
    assert "ms" in res.get_json()["error"]


def test_transcribe_happy_path(client, cfg, monkeypatch):
    _enable_stt(client)
    _write_model(cfg, PARAKEET_ID)
    adapter = _FakeAdapter("good morning")
    monkeypatch.setattr(onnx_asr, "load_model", lambda *a, **k: adapter)
    import base64

    audio = base64.b64encode(_wav(_sine(0.5))).decode()
    res = client.post("/api/stt/transcribe", json={"audio": audio, "model": PARAKEET_ID})
    assert res.status_code == 200
    assert res.get_json() == {"text": "good morning"}
    assert adapter.calls[0][0].size == 0.5 * SAMPLE_RATE


# --- barge-in (interruption) -----------------------------------------------------------


class _LocalBackend(ModelBackend):
    """Offline backend: one text event then done (no network in tests)."""

    def stream(self, messages, tools, model):
        yield ModelEvent("text", text="partial ")
        yield ModelEvent("done")


def test_barge_in_abort_endpoint(client, app, cfg):
    """STT barge-in path over HTTP: the mic's speech-start handler stops TTS
    and POSTs to the abort endpoint. An in-flight turn is cancelled,
    delivers nothing further, and persists no partial assistant reply."""
    manager = app.extensions["gremlin_manager"]
    manager._backend = _LocalBackend()
    sid = client.post("/api/sessions", json={"title": "t"}).get_json()["id"]
    settings = client.get("/api/settings").get_json()

    gen = manager.run(sid, "first message", settings)
    assert next(gen) == {"type": "text", "text": "partial "}

    res = client.post(f"/api/sessions/{sid}/abort")  # what stt.js does on speech start
    assert res.status_code == 200
    assert res.get_json() == {"ok": True, "cancelled": True}

    assert list(gen) == [], "interrupted turn must deliver nothing further"
    stored = client.get(f"/api/sessions/{sid}").get_json()["messages"]
    assert [m["role"] for m in stored] == ["user"], "no partial assistant reply"


def test_barge_in_new_turn_supersedes(client, app, cfg):
    """Continuous-mode auto-send while a turn is active: the new turn claims
    the session and the superseded run stops without persisting its partial
    reply (the old user message stays as context)."""
    manager = app.extensions["gremlin_manager"]
    manager._backend = _LocalBackend()
    sid = client.post("/api/sessions", json={"title": "t"}).get_json()["id"]
    settings = client.get("/api/settings").get_json()

    gen = manager.run(sid, "old message", settings)
    assert next(gen) == {"type": "text", "text": "partial "}

    events = list(manager.run(sid, "new message", settings))
    assert [e["type"] for e in events] == ["text", "done"]
    assert list(gen) == [], "superseded run must stop cleanly"

    stored = client.get(f"/api/sessions/{sid}").get_json()["messages"]
    assert [m["content"] for m in stored] == ["old message", "new message", "partial "]


# --- download helper ----------------------------------------------------------------


class _FakeResp:
    def __init__(self, data: bytes):
        self._data = data

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._data


def test_model_urls():
    m = model_by_id("nemo-parakeet-tdt-0.6b-v2")
    urls = model_urls("nemo-parakeet-tdt-0.6b-v2")
    assert [f for f, _ in urls] == list(m.files)
    assert urls[0] == (
        "config.json",
        "https://huggingface.co/istupakov/parakeet-tdt-0.6b-v2-onnx/resolve/main/config.json",
    )
    w = model_by_id("base")
    wu = model_urls("base")
    assert wu[0] == (
        "config.json",
        "https://huggingface.co/Systran/faster-whisper-base/resolve/main/config.json",
    )
    with pytest.raises(ValueError):
        model_urls("whisper-huge")


def test_download_model_creates_files(tmp_path):
    calls: list[str] = []

    def opener(url):
        calls.append(url)
        return _FakeResp(b"x")

    out = download_model("base", tmp_path, opener=opener)
    m = model_by_id("base")
    assert set(out) == set(m.files)
    assert all(v == "ok" for v in out.values())
    assert calls == [u for _, u in model_urls("base")]
    for f in m.files:
        assert (tmp_path / "base" / f).read_bytes() == b"x"


def test_download_model_skips_existing(tmp_path):
    (tmp_path / "base").mkdir(parents=True)
    (tmp_path / "base" / "model.bin").write_bytes(b"keep")
    calls: list[str] = []

    def opener(url):
        calls.append(url)
        return _FakeResp(b"x")

    out = download_model("base", tmp_path, opener=opener)
    assert out["model.bin"] == "skipped"
    assert "model.bin" not in [u.rsplit("/", 1)[-1] for u in calls]
    assert (tmp_path / "base" / "model.bin").read_bytes() == b"keep"


def test_download_model_unknown_id_raises(tmp_path):
    with pytest.raises(ValueError):
        download_model("whisper-huge", tmp_path, opener=lambda url: _FakeResp(b""))


# --- frontend stt.js (real file, under Node) ---------------------------------------------


# Fakes: mic, AudioContext, TTS (barge-in target), fetch. The harness drives
# the real ScriptProcessor onaudioprocess callback with synthetic buffers and
# verifies threshold gating, silence-based end-of-utterance, the barge-in
# hook, the WAV payload, and hold-vs-continuous auto-send semantics.
_NODE_HARNESS = r"""
;(async () => {
  const tick = (ms = 30) => new Promise((r) => setTimeout(r, ms));
  const fail = (msg) => { console.error("FAIL " + msg); process.exit(1); };

  const proc = { onaudioprocess: null, connect() {}, disconnect() {} };
  Object.defineProperty(globalThis, "window", {
    configurable: true,
    writable: true,
    value: {
      AudioContext: class {
        constructor() {
          this.state = "running";
          this.sampleRate = 16000;
          this.destination = {};
        }
        createMediaStreamSource() { return { connect() {} }; }
        createAnalyser() { return { fftSize: 0, connect() {} }; }
        createScriptProcessor() { return proc; }
        createGain() { return { gain: { value: 0 }, connect() {} }; }
        async resume() {}
        async close() {}
      },
    },
  });
  Object.defineProperty(globalThis, "navigator", {
    configurable: true,
    writable: true,
    value: { mediaDevices: { getUserMedia: async () => ({ getTracks: () => [{ stop() {} }] }) } },
  });
  globalThis.tts = { playing: false, stop() {} };

  const fetched = [];
  const utterances = [];
  let speechStarts = 0;
  globalThis.fetch = (url, opts) => {
    fetched.push({ url, body: JSON.parse(opts.body) });
    return Promise.resolve({ ok: true, status: 200, json: async () => ({ text: "hello there" }) });
  };

  const ev = (amp) => {
    const d = new Float32Array(4096);
    for (let i = 0; i < d.length; i++) d[i] = amp * Math.sin(i / 20);
    return { inputBuffer: { getChannelData: () => d } };
  };
  const feed = (amp) => { if (proc.onaudioprocess) proc.onaudioprocess(ev(amp)); };

  stt.onSpeechStart = () => { speechStarts++; };
  stt.onUtterance = (text, autoSend) => { utterances.push({ text, autoSend }); };
  stt.onStateChange = () => {};
  stt.onError = (m) => fail("stt error: " + m);

  // --- Continuous: quiet noise below the threshold is ignored -------------
  stt.configure({ enabled: true, mode: "continuous", model: "base", threshold: 0.1, silence: 0.25 });
  await stt.continuousToggle();
  await tick();
  if (!stt.listening) fail("not listening after toggle");
  for (let i = 0; i < 20; i++) feed(0.01); // RMS ~0.007 < 0.1
  if (fetched.length !== 0) fail("quiet audio must not trigger transcription");
  if (speechStarts !== 0) fail("quiet audio must not fire speechStart");

  // --- Crossing the threshold starts an utterance and fires the hook ------
  feed(0.5);
  if (speechStarts !== 1) fail("speechStart must fire once on threshold cross");
  if (stt.state !== "recording") fail("state must be 'recording' during speech");
  for (let i = 0; i < 8; i++) feed(0.5); // ~2 s of speech

  // --- Silence past the configured duration ends the utterance ------------
  for (let i = 0; i < 40; i++) { feed(0.01); await tick(10); } // ~0.4 s silence
  await tick();
  if (fetched.length !== 1) fail("expected exactly one transcription, got " + fetched.length);
  const wav = Buffer.from(fetched[0].body.audio, "base64");
  if (fetched[0].body.model !== "base") fail("model missing from body");
  if (wav.toString("ascii", 0, 4) !== "RIFF") fail("not RIFF");
  if (wav.toString("ascii", 8, 12) !== "WAVE") fail("not WAVE");
  if (wav.readUInt16LE(20) !== 1) fail("not PCM");
  if (wav.readUInt16LE(22) !== 1) fail("not mono");
  if (wav.readUInt32LE(24) !== 16000) fail("not 16 kHz");
  if (wav.readUInt16LE(34) !== 16) fail("not 16-bit");
  if (utterances.length !== 1) fail("utterance hook did not fire");
  if (utterances[0].text !== "hello there") fail("transcript not delivered");
  if (utterances[0].autoSend !== true) fail("continuous mode must auto-send");

  // --- Listening resumes: a second utterance is detected ------------------
  feed(0.5);
  if (speechStarts !== 2) fail("listening must resume for the next utterance");
  stt.continuousToggle(); // tap again to stop
  if (stt.listening) fail("toggle must stop listening");
  feed(0.5);
  if (fetched.length !== 1) fail("no capture after stopping");

  // --- Hold mode: capture between press and release, never auto-send ------
  stt.configure({ enabled: true, mode: "hold", model: "base", threshold: 0.1, silence: 0.25 });
  await stt.holdStart();
  await tick();
  if (stt.state !== "recording") fail("hold must record immediately");
  if (speechStarts !== 3) fail("hold press must fire the barge-in hook");
  for (let i = 0; i < 4; i++) feed(0.5); // ~1 s of speech
  stt.holdEnd();
  await tick(50);
  if (fetched.length !== 2) fail("hold must transcribe on release");
  if (utterances.length !== 2) fail("hold utterance hook did not fire");
  if (utterances[1].autoSend !== false) fail("hold mode must not auto-send");

  // --- A quiet hold capture is dropped, not transcribed --------------------
  await stt.holdStart();
  await tick();
  feed(0.01);
  feed(0.01);
  stt.holdEnd();
  await tick(50);
  if (fetched.length !== 2) fail("quiet hold capture must not be transcribed");

  console.log("OK");
})().catch((e) => { console.error("FAIL " + (e && e.message)); process.exit(1); });
"""


def test_frontend_stt_threshold_silence_and_hold(tmp_path):
    """Run the real stt.js under Node; verify threshold gating, silence
    end-of-utterance, barge-in hook, WAV payload, hold vs continuous."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    stt_js = Path(__file__).resolve().parent.parent / "frontend" / "static" / "js" / "stt.js"
    script = tmp_path / "stt_test.js"
    script.write_text(stt_js.read_text() + "\n" + _NODE_HARNESS)
    proc = subprocess.run([node, str(script)], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, f"stdout={proc.stdout}\nstderr={proc.stderr}"
    assert "OK" in proc.stdout
