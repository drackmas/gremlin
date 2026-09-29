"""Plain-text and markdown parser with transcript-header detection.

Transcript files (YouTube captions) start with a short header block::

    Video: Blue Jelly Balls Falling From Sky
    Channel: EYA Censored | Supernatural Bible Changes
    Uploaded: 2024-10-03
    Source: auto (en)
    URL: https://www.youtube.com/watch?v=D0FHu4DuHEw

The header supplies title/author/date/url and marks the file a transcript.
Files without such a header are plain documents (``source_type='txt'``).
"""
from __future__ import annotations

import re
from pathlib import Path

from ..metadata import derive_metadata
from .models import ParsedDoc, doc_id_for, sha256_of

#: header keys, in preference order for the title
_TITLE_KEYS = ("video", "title")
_KEY_RE = re.compile(
    r"^(video|title|channel|uploaded|date|source|url)\s*:\s*(.+)$", re.IGNORECASE
)
_URL_RE = re.compile(r"https?://\S+")
_MAX_HEADER_LINES = 12


def _split_header(lines: list[str]) -> tuple[dict[str, str], int]:
    """Collect header key/value lines from the top of the file.

    Returns ``(header, body_start_index)``. The header is the run of leading
    lines (any of them) matching a known key, ending at the first blank line
    after the first match or at the first non-matching content line.
    """
    header: dict[str, str] = {}
    body_start = 0
    seen = False
    for i, line in enumerate(lines[:_MAX_HEADER_LINES]):
        m = _KEY_RE.match(line.strip())
        if m:
            key = m.group(1).lower()
            header.setdefault(key, m.group(2).strip())
            seen = True
            body_start = i + 1
        elif not line.strip() and seen:
            body_start = i + 1
            break
        elif line.strip() and seen:
            break
        # leading non-header lines before any match are left in the body
    if not seen:
        return {}, 0
    return header, body_start


def parse_text_file(path: Path, source_root: Path) -> ParsedDoc:
    """Parse a .txt/.md file to markdown."""
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    header, body_start = _split_header(lines)
    body = "\n".join(lines[body_start:]).strip()
    if not body:
        body = text.strip()

    url = header.get("url", "")
    if not url:
        m = _URL_RE.search("\n".join(lines[:3]))
        if m:
            url = m.group(0).strip().rstrip(").,")

    meta = derive_metadata(path)
    if header:
        for key in _TITLE_KEYS:
            if header.get(key):
                meta.title = header[key]
                break
        meta.author = header.get("channel", "") or meta.author
        meta.date = header.get("uploaded", "") or header.get("date", "") or meta.date
        if url:
            meta.extra["url"] = url
    elif url:
        meta.extra["url"] = url

    is_transcript = bool(header) or bool(url)
    return ParsedDoc(
        doc_id=doc_id_for(path.relative_to(source_root).as_posix()),
        source_path=path.relative_to(source_root).as_posix(),
        source_type="transcript" if is_transcript else "txt",
        size_bytes=path.stat().st_size,
        markdown=body,
        sha256=sha256_of(path),
        meta=meta,
    )
