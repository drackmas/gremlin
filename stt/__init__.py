"""Local speech-to-text (faster-whisper + Parakeet-TDT via onnx-asr).

Mirrors the :mod:`tts` package: a model catalog (:data:`MODELS`), a
thread-safe lazily-loading engine (:class:`STTEngine`), and a download helper
(:func:`download_model`). Nothing is loaded at import time.
"""

from .models import (
    DEFAULT_STT_MODEL,
    ENGINES,
    MODELS,
    PARAKEET_FILES,
    STTModel,
    STT_MODEL_IDS,
    WHISPER_FILES,
    WHISPER_FILES_LARGE,
    model_by_id,
)
from .engine import (
    MAX_AUDIO_S,
    MIN_AUDIO_MS,
    SAMPLE_RATE,
    STTEngine,
    STTModelError,
)
from .download import STT_BASE_URL, download_model, model_url, model_urls

__all__ = [
    "DEFAULT_STT_MODEL",
    "ENGINES",
    "MODELS",
    "PARAKEET_FILES",
    "WHISPER_FILES",
    "WHISPER_FILES_LARGE",
    "STTModel",
    "STT_MODEL_IDS",
    "model_by_id",
    "MAX_AUDIO_S",
    "MIN_AUDIO_MS",
    "SAMPLE_RATE",
    "STTEngine",
    "STTModelError",
    "STT_BASE_URL",
    "download_model",
    "model_url",
    "model_urls",
]
