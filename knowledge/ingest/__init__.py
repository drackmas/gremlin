"""Ingest: parse source files to clean markdown.

Dispatch by extension; every parser returns a :class:`ParsedDoc` or raises
:class:`IngestError` (sync reports failures loudly, never skips them).

    from knowledge.ingest import parse_file, source_type_for

Supported: .txt .md .epub .docx .doc .pdf .mp3 .wav .m4a .mp4 .webm
"""
from __future__ import annotations

from pathlib import Path

from ..config import KnowledgeConfig
from .models import IngestError, ParsedDoc, doc_id_for, sha256_of

__all__ = [
    "IngestError",
    "ParsedDoc",
    "SUPPORTED_EXTENSIONS",
    "MEDIA_EXTENSIONS",
    "TEXT_EXTENSIONS",
    "doc_id_for",
    "parse_file",
    "sha256_of",
    "source_type_for",
]

SUPPORTED_EXTENSIONS: dict[str, str] = {
    ".txt": "text",
    ".md": "text",
    ".epub": "epub",
    ".docx": "docx",
    ".doc": "doc",
    ".pdf": "pdf",
    ".mp3": "audio",
    ".wav": "audio",
    ".m4a": "audio",
    ".mp4": "video",
    ".webm": "video",
}


TEXT_EXTENSIONS: frozenset[str] = frozenset(
    ext for ext, t in SUPPORTED_EXTENSIONS.items() if t == "text"
)
MEDIA_EXTENSIONS: frozenset[str] = frozenset(
    ext for ext, t in SUPPORTED_EXTENSIONS.items() if t in ("audio", "video")
)


def source_type_for(path: Path) -> str | None:
    """Parser family for *path* (None = unsupported extension)."""
    return SUPPORTED_EXTENSIONS.get(path.suffix.lower())


def parse_file(path: Path, source_root: Path, cfg: KnowledgeConfig) -> ParsedDoc:
    """Parse one source file into markdown + metadata."""
    family = source_type_for(path)
    if family is None:
        raise IngestError(f"{path.name}: unsupported extension {path.suffix}")
    if not path.is_file():
        raise IngestError(f"{path.name}: file not found")
    if family == "text":
        from .text import parse_text_file
        return parse_text_file(path, source_root)
    if family == "epub":
        from .epub import parse_epub_file
        return parse_epub_file(path, source_root)
    if family == "docx":
        from .docx import parse_docx_file
        return parse_docx_file(path, source_root)
    if family == "doc":
        from .doc import parse_doc_file
        return parse_doc_file(path, source_root)
    if family == "pdf":
        from .pdf import parse_pdf_file
        return parse_pdf_file(path, source_root, cfg)
    if family in ("audio", "video"):
        from .media import parse_media_file
        return parse_media_file(path, source_root, cfg, family)
    raise IngestError(f"{path.name}: unknown parser family {family!r}")
