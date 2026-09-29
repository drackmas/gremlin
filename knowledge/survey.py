"""Source-folder survey: what is there, and what can we parse.

Run:  python -m knowledge.survey

Scans the configured source folder (``library/``) and reports, per file:
type, size, derived metadata (title/date/topic), and a cheap parseability
probe. Unsupported or broken files are listed, never skipped silently.
The full report is written to ``data/knowledge/survey.json``.
"""
from __future__ import annotations

import json
import shutil
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path

from .config import KnowledgeConfig
from .ingest import (
    MEDIA_EXTENSIONS,
    TEXT_EXTENSIONS,
    source_type_for,
)
from .metadata import derive_metadata


@dataclass
class SurveyEntry:
    relpath: str
    source_type: str            # "" = unsupported
    size_bytes: int
    title: str
    date: str
    author: str
    topic: str
    parseable: bool
    note: str


def _probe(path: Path, stype: str) -> tuple[bool, str]:
    """Cheap parseability probe (no transcription, no full parse)."""
    try:
        if path.suffix.lower() in TEXT_EXTENSIONS:
            raw = path.read_bytes()[: 64 * 1024]
            raw.decode("utf-8")
            return True, ""
        if stype == "book" and path.suffix.lower() == ".pdf":
            import pymupdf

            with pymupdf.open(path) as doc:
                pages = doc.page_count
                sample = doc.load_page(0).get_text("text") if pages else ""
            if not sample.strip():
                return True, f"{pages} pages, no extractable text (OCR fallback at sync)"
            return True, f"{pages} pages"
        if stype == "book" and path.suffix.lower() == ".epub":
            if not zipfile.is_zipfile(path):
                return False, "not a zip container"
            with zipfile.ZipFile(path) as z:
                if "META-INF/container.xml" not in z.namelist():
                    return False, "no META-INF/container.xml"
            return True, ""
        if path.suffix.lower() == ".docx":
            if not zipfile.is_zipfile(path):
                return False, "not a zip container"
            with zipfile.ZipFile(path) as z:
                if "word/document.xml" not in z.namelist():
                    return False, "no word/document.xml"
            return True, ""
        if path.suffix.lower() == ".doc":
            if shutil.which("soffice") or shutil.which("libreoffice"):
                return True, "converted via LibreOffice at sync"
            return False, "no LibreOffice for legacy .doc"
        if stype in ("audio", "video"):
            if path.stat().st_size == 0:
                return False, "empty file"
            return True, "transcribed via faster-whisper at sync"
    except Exception as e:  # probe must never crash the survey
        return False, f"{type(e).__name__}: {e}"
    return True, ""


def survey(kcfg: KnowledgeConfig) -> list[SurveyEntry]:
    """Scan ``kcfg.source_dir``; returns one entry per file (sorted)."""
    entries: list[SurveyEntry] = []
    if not kcfg.source_dir.is_dir():
        return entries
    for path in sorted(kcfg.source_dir.rglob("*")):
        if not path.is_file() or path.name.startswith("."):
            continue
        rel = path.relative_to(kcfg.source_dir)
        if any(part == "__pycache__" or part.endswith(".pyc") for part in rel.parts):
            continue
        if path.suffix.lower() in TEXT_EXTENSIONS and path.name.endswith(".meta"):
            continue  # sidecars belong to their sibling file
        stype = source_type_for(path)
        meta = derive_metadata(path)
        if not stype:
            parseable, note = False, "unsupported extension"
        elif path.suffix.lower() in MEDIA_EXTENSIONS and path.stat().st_size < 1024:
            parseable, note = False, "suspiciously small media file"
        else:
            parseable, note = _probe(path, stype)
        entries.append(
            SurveyEntry(
                relpath=str(rel),
                source_type=stype or "unknown",
                size_bytes=path.stat().st_size,
                title=meta.title,
                date=meta.date,
                author=meta.author,
                topic=meta.topic,
                parseable=parseable,
                note=note,
            )
        )
    return entries


def _print_report(entries: list[SurveyEntry], out_path: Path) -> None:
    by_type: dict[str, int] = {}
    for e in entries:
        key = e.source_type or "unsupported"
        by_type[key] = by_type.get(key, 0) + 1

    print(f"{'RELPATH':<72} {'TYPE':<11} {'KB':>8}  PARSE  NOTE/TITLE")
    print("-" * 150)
    for e in entries:
        name = e.relpath if len(e.relpath) <= 71 else "…" + e.relpath[-70:]
        parse = "ok" if e.parseable else "FAIL"
        title = e.note or e.title
        if e.date:
            title = f"{title} [{e.date}]"
        if len(title) > 40:
            title = title[:39] + "…"
        print(f"{name:<72} {e.source_type or '-':<11} {e.size_bytes / 1024:>8.1f}  {parse:<6}  {title}")

    print("-" * 150)
    summary = ", ".join(f"{k}: {v}" for k, v in sorted(by_type.items()))
    total = sum(e.size_bytes for e in entries) / (1024 * 1024)
    print(f"files: {len(entries)}  ({summary})  total: {total:.1f} MB")
    print(f"report written: {out_path}")


def main() -> None:
    from config import AppConfig, ensure_dirs

    cfg = AppConfig()
    ensure_dirs(cfg)
    kcfg = KnowledgeConfig(
        root=cfg.knowledge_dir,
        source_dir=cfg.knowledge_source_dir,
        models_dir=cfg.models_dir,
    )
    entries = survey(kcfg)
    out = kcfg.root / "survey.json"
    out.write_text(json.dumps([asdict(e) for e in entries], indent=2), encoding="utf-8")
    _print_report(entries, out)


if __name__ == "__main__":
    main()
