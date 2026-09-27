"""Audio/video transcription: ffmpeg -> faster-whisper (CPU, int8).

Output is one ``[HH:MM:SS] text`` line per whisper segment; the chunker
splits on timestamp lines so citations carry exact offsets. The whisper
model is the local ``Systran/faster-whisper-*`` CT2 model (HF cache;
``transcribe_model`` names it).
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

from ..config import KnowledgeConfig
from ..metadata import derive_metadata
from .models import IngestError, ParsedDoc, doc_id_for, sha256_of

_WAV_RATE = 16_000


def _to_wav(path: Path, out_dir: Path) -> Path:
    """Extract a 16 kHz mono WAV track from any ffmpeg-readable media file."""
    if not shutil.which("ffmpeg"):
        raise IngestError(f"{path.name}: ffmpeg not found (required for media)")
    wav = out_dir / "audio.wav"
    proc = subprocess.run(
        [
            "ffmpeg", "-v", "error", "-y", "-i", str(path),
            "-vn", "-ac", "1", "-ar", str(_WAV_RATE), "-c:a", "pcm_s16le",
            str(wav),
        ],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0 or not wav.is_file() or wav.stat().st_size < 1024:
        raise IngestError(
            f"{path.name}: ffmpeg could not extract audio: "
            f"{proc.stderr.strip()[:300]}"
        )
    return wav


def transcribe(wav: Path, cfg: KnowledgeConfig) -> tuple[str, float]:
    """Run faster-whisper over a 16 kHz mono WAV; returns (markdown, duration)."""
    try:
        from faster_whisper import WhisperModel
    except ImportError as e:
        raise IngestError("faster-whisper is not installed") from e
    model = WhisperModel(
        cfg.transcribe_model, device="cpu", compute_type="int8"
    )
    segments, info = model.transcribe(
        str(wav),
        language=cfg.transcribe_language,
        vad_filter=True,
        beam_size=1,
    )
    lines: list[str] = []
    for seg in segments:
        h, rem = divmod(int(seg.start), 3600)
        m, s = divmod(rem, 60)
        text = seg.text.strip()
        if text:
            lines.append(f"[{h:02d}:{m:02d}:{s:02d}] {text}")
    if not lines:
        raise IngestError("transcription produced no speech segments")
    return "\n".join(lines), info.duration


def parse_media_file(
    path: Path, source_root: Path, cfg: KnowledgeConfig, source_type: str
) -> ParsedDoc:
    """Transcribe an audio/video file to timestamped markdown."""
    with tempfile.TemporaryDirectory(prefix="gremlin-media-") as tmp:
        wav = _to_wav(path, Path(tmp))
        markdown, duration = transcribe(wav, cfg)
    return ParsedDoc(
        doc_id=doc_id_for(path.relative_to(source_root).as_posix()),
        source_path=path.relative_to(source_root).as_posix(),
        source_type=source_type,  # "audio" | "video"
        size_bytes=path.stat().st_size,
        markdown=markdown,
        sha256=sha256_of(path),
        meta=derive_metadata(path),
        duration_sec=round(duration, 1),
        warnings=[f"transcribed with faster-whisper {cfg.transcribe_model} (int8, CPU)"],
    )
