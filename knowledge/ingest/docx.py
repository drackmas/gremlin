"""DOCX parser: zip + lxml over word/document.xml (no python-docx).

Paragraphs become lines; Word heading styles (Heading1..9, Title) become
markdown ``#`` levels so the chunker can cite sections.
"""
from __future__ import annotations

import re
import zipfile
from pathlib import Path

from lxml import etree

from ..metadata import derive_metadata
from .models import ParsedDoc, doc_id_for, sha256_of

_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_HEADING_RE = re.compile(r"^(?:heading|title)(\d?)", re.IGNORECASE)


def _heading_level(p) -> int:
    """Markdown heading level for a paragraph (0 = body text)."""
    pPr = p.find(f"{{{_W}}}pPr")
    if pPr is None:
        return 0
    style = pPr.find(f"{{{_W}}}pStyle")
    if style is None:
        return 0
    m = _HEADING_RE.match(style.get(f"{{{_W}}}val", ""))
    if not m:
        return 0
    if m.group(1):
        return max(1, min(6, int(m.group(1))))
    return 1  # 'Title'


def _para_text(p) -> str:
    return " ".join(
        " ".join("".join(t.itertext()).split()) for t in p.findall(f".//{{{_W}}}t")
    )


def parse_docx_file(path: Path, source_root: Path) -> ParsedDoc:
    """Parse a .docx file to markdown."""
    with zipfile.ZipFile(path) as zf:
        if "word/document.xml" not in zf.namelist():
            from .models import IngestError
            raise IngestError(f"{path.name}: not a DOCX (no word/document.xml)")
        root = etree.fromstring(zf.read("word/document.xml"))
    body = root.find(f"{{{_W}}}body")
    if body is None:
        from .models import IngestError
        raise IngestError(f"{path.name}: empty DOCX body")
    blocks: list[str] = []
    for el in body:
        tag = etree.QName(el).localname
        if tag == "p":
            text = _para_text(el).strip()
            if not text:
                continue
            level = _heading_level(el)
            blocks.append(f"{'#' * level} {text}" if level else text)
        elif tag == "tbl":
            for row in el.findall(f".//{{{_W}}}tr"):
                cells = [
                    " ".join("".join(c.itertext()).split())
                    for c in row.findall(f".//{{{_W}}}tc")
                ]
                line = " | ".join(c for c in cells if c)
                if line:
                    blocks.append(line)
    markdown = "\n\n".join(blocks).strip()
    if not markdown:
        from .models import IngestError
        raise IngestError(f"{path.name}: DOCX contains no text")
    return ParsedDoc(
        doc_id=doc_id_for(path.relative_to(source_root).as_posix()),
        source_path=path.relative_to(source_root).as_posix(),
        source_type="docx",
        size_bytes=path.stat().st_size,
        markdown=markdown,
        sha256=sha256_of(path),
        meta=derive_metadata(path),
    )
