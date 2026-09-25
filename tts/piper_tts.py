"""Server-side Piper TTS.

Loads local Piper ONNX voices from ``<root>/models/piper/`` and synthesizes
raw s16le mono PCM on demand. Voices are listed in :data:`VOICES`; each one
is loaded lazily on first use and cached, guarded by a lock so concurrent
requests are serialized through the (non-thread-safe) ONNX session.

The model is intentionally NOT loaded at import time or at boot: it is
loaded lazily on the first ``ensure_ready`` / ``synthesize`` call, so a
missing model degrades to a clean 503 from the endpoint rather than crashing
the app. No WAV files are ever written to disk; the browser only receives
streamed PCM.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Generator

from .sanitize import sanitize_for_speech

log = logging.getLogger("gremlin.tts")


@dataclass(frozen=True)
class Voice:
    """A Piper voice: identifier, human label, and file names (under
    ``<root>/models/piper/``)."""

    id: str
    label: str
    model_file: str
    config_file: str


# Available voices (HuggingFace rhasspy/piper-voices, v1.0.0 layout).
VOICES: tuple[Voice, ...] = (
    Voice(
        "en_GB-alba-medium",
        "Alba (en-GB, medium)",
        "en_GB-alba-medium.onnx",
        "en_GB-alba-medium.onnx.json",
    ),
    Voice(
        "en_GB-alan-medium",
        "Alan (en-GB, medium)",
        "en_GB-alan-medium.onnx",
        "en_GB-alan-medium.onnx.json",
    ),
    Voice(
        "en_GB-cori-high",
        "Cori (en-GB, high)",
        "en_GB-cori-high.onnx",
        "en_GB-cori-high.onnx.json",
    ),
    Voice(
        "en_GB-jenny_dioco-medium",
        "Jenny Dioco (en-GB, medium)",
        "en_GB-jenny_dioco-medium.onnx",
        "en_GB-jenny_dioco-medium.onnx.json",
    ),
    Voice(
        "en_GB-northern_english_male-medium",
        "Northern English Male (en-GB, medium)",
        "en_GB-northern_english_male-medium.onnx",
        "en_GB-northern_english_male-medium.onnx.json",
    ),
    Voice(
        "en_GB-semaine-medium",
        "Semaine (en-GB, medium)",
        "en_GB-semaine-medium.onnx",
        "en_GB-semaine-medium.onnx.json",
    ),
    Voice(
        "en_GB-southern_english_female-low",
        "Southern English Female (en-GB, low)",
        "en_GB-southern_english_female-low.onnx",
        "en_GB-southern_english_female-low.onnx.json",
    ),
    Voice(
        "en_GB-vctk-medium",
        "VCTK (en-GB, medium)",
        "en_GB-vctk-medium.onnx",
        "en_GB-vctk-medium.onnx.json",
    ),
    Voice(
        "en_US-amy-medium",
        "Amy (en-US, medium)",
        "en_US-amy-medium.onnx",
        "en_US-amy-medium.onnx.json",
    ),
    Voice(
        "en_US-arctic-medium",
        "Arctic (en-US, medium)",
        "en_US-arctic-medium.onnx",
        "en_US-arctic-medium.onnx.json",
    ),
    Voice(
        "en_US-bryce-medium",
        "Bryce (en-US, medium)",
        "en_US-bryce-medium.onnx",
        "en_US-bryce-medium.onnx.json",
    ),
    Voice(
        "en_US-danny-low",
        "Danny (en-US, low)",
        "en_US-danny-low.onnx",
        "en_US-danny-low.onnx.json",
    ),
    Voice(
        "en_US-hfc_female-medium",
        "HFC Female (en-US, medium)",
        "en_US-hfc_female-medium.onnx",
        "en_US-hfc_female-medium.onnx.json",
    ),
    Voice(
        "en_US-hfc_male-medium",
        "HFC Male (en-US, medium)",
        "en_US-hfc_male-medium.onnx",
        "en_US-hfc_male-medium.onnx.json",
    ),
    Voice(
        "en_US-joe-medium",
        "Joe (en-US, medium)",
        "en_US-joe-medium.onnx",
        "en_US-joe-medium.onnx.json",
    ),
    Voice(
        "en_US-john-medium",
        "John (en-US, medium)",
        "en_US-john-medium.onnx",
        "en_US-john-medium.onnx.json",
    ),
    Voice(
        "en_US-kathleen-low",
        "Kathleen (en-US, low)",
        "en_US-kathleen-low.onnx",
        "en_US-kathleen-low.onnx.json",
    ),
    Voice(
        "en_US-kristin-medium",
        "Kristin (en-US, medium)",
        "en_US-kristin-medium.onnx",
        "en_US-kristin-medium.onnx.json",
    ),
    Voice(
        "en_US-kusal-medium",
        "Kusal (en-US, medium)",
        "en_US-kusal-medium.onnx",
        "en_US-kusal-medium.onnx.json",
    ),
    Voice(
        "en_US-l2arctic-medium",
        "L2Arctic (en-US, medium)",
        "en_US-l2arctic-medium.onnx",
        "en_US-l2arctic-medium.onnx.json",
    ),
    Voice(
        "en_US-lessac-medium",
        "Lessac (en-US, medium)",
        "en_US-lessac-medium.onnx",
        "en_US-lessac-medium.onnx.json",
    ),
    Voice(
        "en_US-libritts-high",
        "LibriTTS (en-US, high)",
        "en_US-libritts-high.onnx",
        "en_US-libritts-high.onnx.json",
    ),
    Voice(
        "en_US-libritts_r-medium",
        "LibriTTS-R (en-US, medium)",
        "en_US-libritts_r-medium.onnx",
        "en_US-libritts_r-medium.onnx.json",
    ),
    Voice(
        "en_US-ljspeech-high",
        "LJSpeech (en-US, high)",
        "en_US-ljspeech-high.onnx",
        "en_US-ljspeech-high.onnx.json",
    ),
    Voice(
        "en_US-mike-medium",
        "Mike (en-US, medium)",
        "en_US-mike-medium.onnx",
        "en_US-mike-medium.onnx.json",
    ),
    Voice(
        "en_US-norman-medium",
        "Norman (en-US, medium)",
        "en_US-norman-medium.onnx",
        "en_US-norman-medium.onnx.json",
    ),
    Voice(
        "en_US-reza_ibrahim-medium",
        "Reza Ibrahim (en-US, medium)",
        "en_US-reza_ibrahim-medium.onnx",
        "en_US-reza_ibrahim-medium.onnx.json",
    ),
    Voice(
        "en_US-ryan-high",
        "Ryan (en-US, high)",
        "en_US-ryan-high.onnx",
        "en_US-ryan-high.onnx.json",
    ),
    Voice(
        "en_US-sam-medium",
        "Sam (en-US, medium)",
        "en_US-sam-medium.onnx",
        "en_US-sam-medium.onnx.json",
    ),
    Voice(
        "jarvis-high",
        "Jarvis (high)",
        "jarvis-high.onnx",
        "jarvis-high.onnx.json",
    ),
    Voice(
        "en_US-glados-high",
        "GLaDOS (en-US, high)",
        "en_US-glados-high.onnx",
        "en_US-glados-high.onnx.json",
    ),
    Voice(
        "en_US-elisa-medium",
        "Elisa (en-US, medium)",
        "en_US-elisa-medium.onnx",
        "en_US-elisa-medium.onnx.json",
    ),
    Voice(
        "jane-eyre",
        "Jane Eyre (en-GB, medium)",
        "piper-onnx-jane-eyre-english-british.onnx",
        "piper-onnx-jane-eyre-english-british.onnx.json",
    ),
)

DEFAULT_VOICE = VOICES[0].id

# Cap on characters accepted per /api/tts/stream request.
MAX_TEXT_CHARS = 2000

# Size (bytes) of each raw-PCM chunk yielded over HTTP.
CHUNK_SIZE = 8192


def voice_by_id(voice_id: str) -> Voice:
    """Look up a :class:`Voice` by id. Raises ``ValueError`` if unknown."""
    for v in VOICES:
        if v.id == voice_id:
            return v
    raise ValueError(f"unknown voice: {voice_id!r}")


class TTSModelError(Exception):
    """Raised when a Piper voice cannot be loaded (missing model, no piper)."""


class PiperTTS:
    """Thread-safe wrapper around lazily loaded, cached ``PiperVoice`` objects.

    Parameters
    ----------
    model_dir:
        Directory containing the ``<voice>.onnx`` / ``<voice>.onnx.json``
        files (``<root>/models/piper``).
    """

    def __init__(self, model_dir: Path) -> None:
        self.model_dir = Path(model_dir)
        self._lock = threading.Lock()
        self._loaded: dict[str, object] = {}      # voice id -> PiperVoice
        self._loaded_rates: dict[str, int] = {}   # voice id -> sample rate (Hz)

    # --- per-voice file helpers -------------------------------------------

    def model_path(self, voice_id: str) -> Path:
        return self.model_dir / voice_by_id(voice_id).model_file

    def config_path(self, voice_id: str) -> Path:
        return self.model_dir / voice_by_id(voice_id).config_file

    def available(self, voice_id: str = DEFAULT_VOICE) -> bool:
        """True when both the model and config files exist on disk."""
        return self.model_path(voice_id).exists() and self.config_path(voice_id).exists()

    def describe_missing(self, voice_id: str = DEFAULT_VOICE) -> str:
        """Names of the files missing for this voice ('' when complete)."""
        missing = [
            p.name
            for p in (self.model_path(voice_id), self.config_path(voice_id))
            if not p.exists()
        ]
        return ", ".join(missing)

    def sample_rate(self, voice_id: str = DEFAULT_VOICE) -> int:
        """Sample rate (Hz) from the config JSON, without loading the ONNX."""
        rate = self._loaded_rates.get(voice_id)
        if rate is not None:
            return rate
        cfg = json.loads(self.config_path(voice_id).read_text())
        rate = int(cfg["audio"]["sample_rate"])
        self._loaded_rates[voice_id] = rate
        return rate

    # --- voice loading ------------------------------------------------------

    def _ensure_voice(self, voice_id: str):
        """Return the cached ``PiperVoice`` for ``voice_id``, loading it if needed."""
        voice = self._loaded.get(voice_id)
        if voice is not None:
            return voice
        if not self.available(voice_id):
            raise TTSModelError(f"Piper model not found: {self.describe_missing(voice_id)}")
        try:
            from piper import PiperVoice
        except ImportError as e:
            raise TTSModelError(
                f"piper-tts is not installed: {e} (run: pip install piper-tts)"
            ) from e
        try:
            voice = PiperVoice.load(
                str(self.model_path(voice_id)),
                config_path=str(self.config_path(voice_id)),
            )
        except Exception as e:
            log.error("failed to load Piper voice %s: %s", voice_id, e)
            raise TTSModelError(f"failed to load Piper voice {voice_id}: {e}") from e
        self._loaded[voice_id] = voice
        self._loaded_rates[voice_id] = int(voice.config.sample_rate)
        log.info("Piper voice loaded: %s (sample_rate=%d)", voice_id, self._loaded_rates[voice_id])
        return voice

    def ensure_ready(self, voice_id: str = DEFAULT_VOICE) -> int:
        """Load the voice if needed. Returns its sample rate in Hz."""
        self._ensure_voice(voice_id)
        return self._loaded_rates[voice_id]

    # --- synthesis ----------------------------------------------------------

    def synthesize_chunks(
        self, text: str, voice_id: str = DEFAULT_VOICE
    ) -> Generator[bytes, None, None]:
        """Yield raw PCM chunks (``CHUNK_SIZE`` bytes) for *text*.

        Text is validated up front (before touching the model), then rewritten
        by :func:`sanitize_for_speech`; if nothing speechable remains (pure
        markdown syntax) the generator yields nothing and never loads a voice.
        Piper yields one chunk per sentence; each is re-sliced to
        ``CHUNK_SIZE`` so the HTTP body streams. The ONNX session is not
        thread-safe, so the whole synthesis (including yields) runs under a
        lock; the lock is released when the generator is exhausted or closed
        (e.g. client disconnect).
        """
        stripped = (text or "").strip()
        if not stripped:
            raise ValueError("text must not be empty")
        if len(stripped) > MAX_TEXT_CHARS:
            raise ValueError(f"text must be at most {MAX_TEXT_CHARS} characters")
        speech = sanitize_for_speech(stripped)
        if not speech:
            return
        with self._lock:
            voice = self._ensure_voice(voice_id)
            for chunk in voice.synthesize(speech):
                pcm = chunk.audio_int16_bytes
                for i in range(0, len(pcm), CHUNK_SIZE):
                    yield pcm[i : i + CHUNK_SIZE]

    def synthesize_pcm(self, text: str, voice_id: str = DEFAULT_VOICE) -> bytes:
        """Synthesize *text* to a single raw s16le mono PCM byte string."""
        return b"".join(self.synthesize_chunks(text, voice_id=voice_id))
