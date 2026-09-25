"""Server-side text-to-speech package.

Provides the Piper engine (multi-voice, see :mod:`tts.piper_tts`) and
:func:`sanitize_for_speech`, which rewrites markdown into plain prose before
synthesis.
"""

from .piper_tts import (
    CHUNK_SIZE,
    DEFAULT_VOICE,
    MAX_TEXT_CHARS,
    VOICES,
    PiperTTS,
    TTSModelError,
    Voice,
    voice_by_id,
)
from .sanitize import sanitize_for_speech
from .download import VOICE_BASE_URL, download_voice, voice_url_parts, voice_urls

__all__ = [
    "PiperTTS",
    "TTSModelError",
    "Voice",
    "VOICES",
    "DEFAULT_VOICE",
    "MAX_TEXT_CHARS",
    "CHUNK_SIZE",
    "VOICE_BASE_URL",
    "sanitize_for_speech",
    "voice_by_id",
    "download_voice",
    "voice_url_parts",
    "voice_urls",
]
