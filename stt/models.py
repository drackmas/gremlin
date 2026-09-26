"""STT model catalog: faster-whisper + NVIDIA Parakeet-TDT.

Two engines, selected via the ``stt_engine`` setting:

* ``"whisper"``  — OpenAI Whisper models run through faster-whisper
  (CTranslate2, int8 on CPU).  Files are the standard
  ``Systran/faster-whisper-*`` Hugging Face layout
  (``model.bin`` + tokenizer/config files).
* ``"parakeet"`` — NVIDIA NeMo Parakeet-TDT 0.6B (``v2`` English,
  ``v3`` multilingual) run through onnx-asr (ONNX Runtime, no torch).

Files live under ``<root>/models/stt/<id>/`` (copied in from the
AIVTuber ``stt_models/`` directory; see ``models/stt/README.md``) and are
loaded lazily by :mod:`stt.engine` — never at import time.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Engines selectable via ``stt_engine``.
ENGINES: tuple[str, ...] = ("whisper", "parakeet")

#: Files on disk for the small faster-whisper models (tiny, base).
WHISPER_FILES: tuple[str, ...] = (
    "config.json",
    "model.bin",
    "tokenizer.json",
    "vocabulary.txt",
)

#: Files on disk for the large faster-whisper models (large-v3, turbo).
WHISPER_FILES_LARGE: tuple[str, ...] = (
    "config.json",
    "model.bin",
    "preprocessor_config.json",
    "tokenizer.json",
    "vocabulary.json",
)

#: Files on disk for every Parakeet-TDT catalog model
#: (relative to ``models/stt/<id>/``).
PARAKEET_FILES: tuple[str, ...] = (
    "config.json",
    "encoder-model.onnx",
    "encoder-model.onnx.data",
    "decoder_joint-model.onnx",
    "nemo128.onnx",
    "vocab.txt",
)


@dataclass(frozen=True)
class STTModel:
    """One STT model in the catalog."""

    id: str
    label: str
    engine: str  # "whisper" | "parakeet"
    repo_id: str  # Hugging Face repo for ``stt.download``
    files: tuple[str, ...]
    approx_mb: int


MODELS: tuple[STTModel, ...] = (
    STTModel(
        "tiny",
        "Whisper Tiny (fastest, ~75 MB)",
        "whisper",
        "Systran/faster-whisper-tiny",
        WHISPER_FILES,
        75,
    ),
    STTModel(
        "base",
        "Whisper Base (good default, ~142 MB)",
        "whisper",
        "Systran/faster-whisper-base",
        WHISPER_FILES,
        142,
    ),
    STTModel(
        "large-v3",
        "Whisper Large v3 (best accuracy, ~3 GB)",
        "whisper",
        "Systran/faster-whisper-large-v3",
        ("config.json", "model.bin", "preprocessor_config.json", "tokenizer.json", "vocabulary.json"),
        3025,
    ),
    STTModel(
        "turbo",
        "Whisper Large v3 Turbo (fast + accurate, ~1.6 GB)",
        "whisper",
        "Systran/faster-whisper-large-v3-turbo",
        ("config.json", "model.bin", "preprocessor_config.json", "tokenizer.json", "vocabulary.json"),
        1580,
    ),
    STTModel(
        "nemo-parakeet-tdt-0.6b-v2",
        "NVIDIA Parakeet-TDT 0.6B v2 (English, ~2.4 GB)",
        "parakeet",
        "istupakov/parakeet-tdt-0.6b-v2-onnx",
        PARAKEET_FILES,
        2400,
    ),
    STTModel(
        "nemo-parakeet-tdt-0.6b-v3",
        "NVIDIA Parakeet-TDT 0.6B v3 (multilingual, ~2.4 GB)",
        "parakeet",
        "istupakov/parakeet-tdt-0.6b-v3-onnx",
        PARAKEET_FILES,
        2400,
    ),
)

#: Model ids, in catalog order.
STT_MODEL_IDS: tuple[str, ...] = tuple(m.id for m in MODELS)

#: Default model (matches AIVTuber: whisper + base).
DEFAULT_STT_MODEL = "base"


def model_by_id(model_id: str) -> STTModel:
    """Return the catalog entry for *model_id*.

    Raises:
        ValueError: when *model_id* is not in the catalog.
    """
    for m in MODELS:
        if m.id == model_id:
            return m
    raise ValueError(f"unknown STT model: {model_id!r} (expected one of: {', '.join(STT_MODEL_IDS)})")
