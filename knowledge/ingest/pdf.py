"""PDF parser: PyMuPDF text extraction, OCR (Tesseract) only as fallback.

Extract-first: every page is text-extracted; a page counts as extractable
when it yields at least ``ocr_min_text_chars`` characters. Failing pages are
rendered to PNG at ``ocr_dpi`` and run through the ``tesseract`` CLI. Any
OCR'd page sets ``ocr_suspect`` so search results warn by default.

Pages are separated by ``<!--page:N-->`` (1-based) for chunking/citations.
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

import fitz  # PyMuPDF

from ..config import KnowledgeConfig
from ..metadata import derive_metadata
from .models import IngestError, ParsedDoc, doc_id_for, sha256_of


def _ocr_page(page, dpi: int, lang: str) -> str:
    """Render one page to PNG and OCR it with the tesseract CLI."""
    if not shutil.which("tesseract"):
        raise IngestError(
            "page needs OCR but the tesseract binary was not found"
        )
    pix = page.get_pixmap(dpi=dpi)
    png = pix.tobytes("png")
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        f.write(png)
        img = Path(f.name)
    try:
        proc = subprocess.run(
            ["tesseract", str(img), "stdout", "--dpi", str(dpi), "-l", lang],
            capture_output=True,
            text=True,
            timeout=600,
        )
        if proc.returncode != 0:
            raise IngestError(
                f"tesseract failed on page: {proc.stderr.strip()[:300]}"
            )
        return proc.stdout
    finally:
        img.unlink(missing_ok=True)


def parse_pdf_file(path: Path, source_root: Path, cfg: KnowledgeConfig) -> ParsedDoc:
    """Parse a PDF to markdown with page markers."""
    try:
        doc = fitz.open(path)
    except Exception as e:
        raise IngestError(f"{path.name}: cannot open PDF: {e}") from e
    parts: list[str] = []
    ocr_pages: list[int] = []
    page_count = doc.page_count
    with doc:
        for i, page in enumerate(doc):
            n = i + 1
            try:
                text = page.get_text("text")
            except Exception:
                text = ""
            if len(text.strip()) >= cfg.ocr_min_text_chars:
                parts.append(f"<!--page:{n}-->\n{text.strip()}")
            else:
                ocr = _ocr_page(page, cfg.ocr_dpi, cfg.ocr_lang)
                parts.append(f"<!--page:{n}-->\n{ocr.strip()}")
                ocr_pages.append(n)
    if ocr_pages and len(ocr_pages) == page_count:
        parts[0] += "\n\n> (OCR: no extractable text layer found in this PDF)"
    markdown = "\n\n".join(parts).strip()
    if not markdown:
        raise IngestError(f"{path.name}: PDF contains no pages")
    warnings = []
    if ocr_pages:
        warnings.append(f"OCR applied to {len(ocr_pages)}/{page_count} pages")
    return ParsedDoc(
        doc_id=doc_id_for(path.relative_to(source_root).as_posix()),
        source_path=path.relative_to(source_root).as_posix(),
        source_type="pdf",
        size_bytes=path.stat().st_size,
        markdown=markdown,
        sha256=sha256_of(path),
        meta=derive_metadata(path),
        page_count=page_count,
        ocr_suspect=bool(ocr_pages),
        warnings=warnings,
    )
