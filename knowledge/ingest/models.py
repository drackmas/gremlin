"""Parsed-document model shared by parsers, chunking, and sync."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

from ..metadata import DocumentMeta


class IngestError(Exception):
    """A source file could not be parsed; sync reports it, never skips it."""


def doc_id_for(source_path: str) -> str:
    """Stable id derived from the file's path relative to the source root."""
    return hashlib.sha1(source_path.encode("utf-8")).hexdigest()[:16]


def sha256_of(path: Path) -> str:
    """Content hash used by the sync manifest (incremental re-ingest)."""
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


@dataclass
class ParsedDoc:
    """One source file after parsing: markdown + metadata + parse flags.

    ``markdown`` is the cleaned intermediate form (the index is rebuildable
    from it). Parsers emit structural markers the chunker understands:
      * pdf/epub pages:  ``<!--page:N-->``
      * media segments:  ``[HH:MM:SS] text`` lines
      * headings:        markdown ``#`` levels (docx styles, epub h1-h6)
    """

    doc_id: str
    source_path: str        # relative to source_dir, '/'-separated
    source_type: str        # transcript | txt | pdf | epub | docx | doc | audio | video
    markdown: str
    sha256: str
    meta: DocumentMeta
    page_count: int = 0     # pdf / epub
    duration_sec: float = 0.0  # audio / video
    ocr_suspect: bool = False  # True when ANY content came from OCR
    size_bytes: int = 0
    warnings: list[str] = field(default_factory=list)
