"""Legacy .doc parser: LibreOffice headless conversion to .docx, then docx."""
from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

from ..metadata import derive_metadata
from .docx import parse_docx_file
from .models import IngestError, ParsedDoc, doc_id_for, sha256_of


def _soffice() -> str | None:
    return shutil.which("soffice") or shutil.which("libreoffice")


def parse_doc_file(path: Path, source_root: Path) -> ParsedDoc:
    """Parse a legacy .doc file via LibreOffice conversion."""
    exe = _soffice()
    if not exe:
        raise IngestError(
            f"{path.name}: .doc requires LibreOffice (soffice) which was not found"
        )
    with tempfile.TemporaryDirectory(prefix="gremlin-doc-") as tmp:
        proc = subprocess.run(
            [exe, "--headless", "--convert-to", "docx", "--outdir", tmp, str(path)],
            capture_output=True,
            text=True,
            timeout=300,
        )
        converted = Path(tmp) / (path.stem + ".docx")
        if proc.returncode != 0 or not converted.is_file():
            raise IngestError(
                f"{path.name}: LibreOffice conversion failed: "
                f"{(proc.stderr or proc.stdout).strip()[:300]}"
            )
        # parse_docx_file computes doc_id/sha from the source path; re-derive
        # here so the id maps to the ORIGINAL file, not the temp conversion.
        doc = parse_docx_file(converted, source_root=Path(tmp))
    return ParsedDoc(
        doc_id=doc_id_for(path.relative_to(source_root).as_posix()),
        source_path=path.relative_to(source_root).as_posix(),
        source_type="doc",
        size_bytes=path.stat().st_size,
        markdown=doc.markdown,
        sha256=sha256_of(path),
        meta=derive_metadata(path),
        warnings=list(doc.warnings) + ["converted from .doc via LibreOffice"],
    )
