"""Server-side STT: faster-whisper and NVIDIA Parakeet-TDT.

Mirrors the AIVTuber design with two backends, selected per model
(:mod:`stt.models`):

* **Whisper** (``tiny`` … ``turbo``): faster-whisper / CTranslate2, int8
  on CPU.  The model directory (``models/stt/<id>/``) is passed straight
  to ``WhisperModel``.
* **Parakeet** (``nemo-parakeet-tdt-0.6b-v2/v3``): onnx-asr / ONNX
  Runtime (no torch).  Loaded with ``onnx_asr.load_model(id, path=dir)``.

Audio arrives as 16-bit WAV (or raw s16le PCM) from the browser, is
decoded to a 16 kHz mono float32 waveform, and transcribed to text on
demand.  Models are loaded lazily on first use and cached; a single lock
serializes concurrent requests through the (non-thread-safe) runtimes.
"""

from __future__ import annotations

import io
import logging
import threading
import wave
from pathlib import Path
from typing import Any

import numpy as np

from .models import DEFAULT_STT_MODEL, model_by_id

log = logging.getLogger("gremlin.stt")

#: Whisper's native input sample rate (Hz).
SAMPLE_RATE = 16_000

#: Utterances shorter than this (ms) are rejected as empty.
MIN_AUDIO_MS = 200

#: Utterances longer than this (seconds) are rejected.
MAX_AUDIO_S = 30

#: Sample rates accepted for numpy waveforms (resampled to 16 kHz).
SUPPORTED_SAMPLE_RATES = (8_000, 11_025, 16_000, 22_050, 24_000, 32_000, 44_100, 48_000)


class STTModelError(Exception):
    """Raised when an STT model cannot be loaded (missing files, no runtime)."""


def _resample(pcm: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
    """Linearly resample a mono float32 waveform from *src_rate* to *dst_rate* (Hz)."""
    if src_rate == dst_rate:
        return pcm
    n = max(1, int(round(len(pcm) * dst_rate / src_rate)))
    pos = np.linspace(0.0, len(pcm) - 1, n)
    i0 = np.floor(pos).astype(int)
    i1 = np.minimum(i0 + 1, len(pcm) - 1)
    frac = (pos - i0).astype(np.float32)
    return (pcm[i0] * (1.0 - frac) + pcm[i1] * frac).astype(np.float32)


def _decode_audio(audio: bytes, sample_rate: int = SAMPLE_RATE) -> tuple[np.ndarray, int]:
    """Decode *audio* (16-bit s16le WAV or raw s16le PCM) to mono float32 in [-1, 1].

    WAV input: 8/16/24/32-bit PCM is normalized to 16-bit, multi-channel audio
    is downmixed by averaging, and anything not at :data:`SAMPLE_RATE` is
    resampled. Non-WAV bytes are treated as raw s16le at *sample_rate*.

    Returns ``(waveform, sample_rate)`` ready for the runtime adapters.

    Raises:
        ValueError: for empty input or an unsupported bit depth / sample rate.
    """
    if not audio:
        raise ValueError("audio must not be empty")
    try:
        with wave.open(io.BytesIO(audio), "rb") as w:
            rate, channels, width = w.getframerate(), w.getnchannels(), w.getsampwidth()
            frames = w.readframes(w.getnframes())
    except (wave.Error, EOFError):
        rate, channels, width, frames = int(sample_rate) or SAMPLE_RATE, 1, 2, bytes(audio)
        if len(frames) % 2:
            raise ValueError("raw audio must be little-endian 16-bit PCM (even number of bytes)")
    if width not in (1, 2, 3, 4):
        raise ValueError(f"unsupported audio bit depth: {width * 8} bit (need 8/16/24/32-bit PCM)")
    if width == 1:
        pcm = (np.frombuffer(frames, dtype=np.uint8).astype(np.int32) * 256 - 32768).astype(np.int16)
    elif width == 2:
        pcm = np.frombuffer(frames, dtype="<i2").astype(np.int16)
    elif width == 3:  # little-endian 24-bit: pad each sample to 4 bytes
        padded = np.pad(np.frombuffer(frames, dtype=np.uint8).reshape(-1, 3), ((0, 0), (0, 1)))
        pcm = (padded.view(np.uint32).astype(np.int32) >> 8).astype(np.int16)
    else:
        pcm = (np.frombuffer(frames, dtype="<i4") >> 16).astype(np.int16)
    if channels > 1:
        n = pcm.size // channels
        pcm = pcm[: n * channels].reshape(n, channels).mean(axis=1).astype(np.int16)
    waveform = pcm.astype(np.float32) / 32768.0
    if rate not in SUPPORTED_SAMPLE_RATES:
        raise ValueError(f"unsupported sample rate: {rate} Hz")
    if rate != SAMPLE_RATE:
        waveform = _resample(waveform, rate, SAMPLE_RATE)
        rate = SAMPLE_RATE
    return waveform, rate


def _min_ms(waveform: np.ndarray) -> int:
    """Duration of *waveform* in milliseconds at :data:`SAMPLE_RATE`."""
    return int(round(len(waveform) / SAMPLE_RATE * 1000))


class STTEngine:
    """Thread-safe wrapper around lazily loaded, cached model adapters."""

    def __init__(self, model_root: str | Path) -> None:
        self.model_root = Path(model_root)
        self._loaded: dict[str, Any] = {}
        self._lock = threading.Lock()

    # --- model availability ------------------------------------------------

    def model_path(self, model_id: str) -> Path:
        """Directory holding *model_id*'s files under the model root."""
        return self.model_root / model_by_id(model_id).id

    def available(self, model_id: str) -> bool:
        """True when every catalog file for *model_id* exists on disk."""
        return all((self.model_path(model_id) / f).is_file() for f in model_by_id(model_id).files)

    def describe_missing(self, model_id: str) -> str:
        """Comma-joined names of the model files that are missing ('' = none)."""
        model = model_by_id(model_id)
        missing = [f for f in model.files if not (self.model_path(model.id) / f).is_file()]
        return ", ".join(missing)

    # --- model loading -----------------------------------------------------

    def _ensure_model(self, model_id: str = DEFAULT_STT_MODEL) -> Any:
        """Return the cached adapter for *model_id*, loading it if needed.

        Raises:
            ValueError: for an unknown model id.
            STTModelError: when files are missing or the model fails to load.
        """
        model = model_by_id(model_id)
        with self._lock:
            adapter = self._loaded.get(model.id)
            if adapter is not None:
                return adapter
            missing = self.describe_missing(model.id)
            if missing:
                raise STTModelError(
                    f"STT model files missing ({missing}); "
                    f"copy them to {self.model_path(model.id)}/ or run: python -m stt.download {model.id}"
                )
            if model.engine == "parakeet":
                adapter = self._load_parakeet(model)
            else:
                adapter = self._load_whisper(model)
            self._loaded[model.id] = adapter
            log.info("STT model loaded: %s (%s)", model.id, model.engine)
            return adapter

    def _load_whisper(self, model) -> object:
        try:
            from faster_whisper import WhisperModel
        except ImportError as e:
            raise STTModelError(f"faster-whisper is not installed: {e} (run: pip install faster-whisper)") from e
        try:
            return WhisperModel(str(self.model_path(model.id)), device="cpu", compute_type="int8")
        except Exception as e:
            log.error("failed to load STT model %s: %s", model.id, e)
            raise STTModelError(f"failed to load STT model {model.id}: {e}") from e

    def _load_parakeet(self, model) -> object:
        try:
            import onnx_asr
        except ImportError as e:
            raise STTModelError(
                f"onnx-asr is not installed: {e} (run: pip install 'onnx-asr[cpu,hub]')"
            ) from e
        try:
            return onnx_asr.load_model(model.id, path=str(self.model_path(model.id)))
        except Exception as e:
            log.error("failed to load STT model %s: %s", model.id, e)
            raise STTModelError(f"failed to load STT model {model.id}: {e}") from e

    def ensure_ready(self, model_id: str = DEFAULT_STT_MODEL) -> None:
        """Load *model_id* up front (no-op when already cached)."""
        self._ensure_model(model_id)

    # --- transcription -----------------------------------------------------

    def transcribe(self, audio: bytes, model_id: str = DEFAULT_STT_MODEL) -> str:
        """Transcribe *audio* (WAV s16le or raw PCM) to text with *model_id*.

        Raises:
            ValueError: empty/unknown model, or audio too short / too long.
            STTModelError: missing model files or a failed model load.
        """
        model = model_by_id(model_id)
        waveform, _rate = _decode_audio(audio)
        ms = _min_ms(waveform)
        if ms < MIN_AUDIO_MS:
            raise ValueError(f"audio too short: {ms} ms (need at least {MIN_AUDIO_MS} ms)")
        if ms > MAX_AUDIO_S * 1000:
            raise ValueError(f"audio too long: {ms} ms (max {MAX_AUDIO_S * 1000} ms)")
        adapter = self._ensure_model(model.id)
        with self._lock:
            if model.engine == "parakeet":
                text = adapter.recognize(waveform, sample_rate=SAMPLE_RATE, channel="mean")
            else:
                segments, _info = adapter.transcribe(waveform, vad_filter=False, beam_size=1)
                text = " ".join(seg.text.strip() for seg in segments)
        return str(text).strip()
