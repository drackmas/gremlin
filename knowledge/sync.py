"""knowledge.sync: source folder -> disposable index (ON-PURPOSE only).

Owner workflow: drop files into the source folder, click SYNC (or run
``python -m knowledge.sync``). The DB is a disposable index: it is always
rebuildable from the source files, so sync is the only writer and it is
never automatic, never file-watched, never run behind the owner's back.

Report: "N parsed / N failed / N flagged" (+ removed when files were
deleted from the source folder). After every sync, sample chunks are
rewritten to ``data/knowledge/samples/`` for human review.
"""
from __future__ import annotations

import logging
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from .chunking import chunk_markdown
from .config import KnowledgeConfig
from .embed import Embedder
from .ingest import IngestError, ParsedDoc, parse_file, sha256_of, source_type_for
from .store import Index

log = logging.getLogger("gremlin.knowledge.sync")


@dataclass
class SyncReport:
    parsed: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)
    flagged: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    seconds: float = 0.0

    def summary(self) -> str:
        parts = [
            f"{len(self.parsed)} parsed",
            f"{len(self.failed)} failed",
            f"{len(self.flagged)} flagged",
        ]
        if self.removed:
            parts.append(f"{len(self.removed)} removed")
        return " / ".join(parts) + f" in {self.seconds:.1f}s"

    def details(self) -> str:
        lines = [self.summary()]
        for rel in self.parsed:
            lines.append(f"  + {rel}")
        for rel, err in self.failed:
            lines.append(f"  FAILED {rel}: {err}")
        for rel in self.flagged:
            lines.append(f"  FLAGGED {rel} (ocr-suspect)")
        for rel in self.removed:
            lines.append(f"  - {rel}")
        return "\n".join(lines)


def source_files(source_dir: Path) -> dict[str, Path]:
    """Every supported file under *source_dir*, keyed by relative path."""
    out: dict[str, Path] = {}
    if not source_dir.is_dir():
        return out
    for p in sorted(source_dir.rglob("*")):
        if p.is_file() and source_type_for(p) is not None:
            out[p.relative_to(source_dir).as_posix()] = p
    return out


def dump_samples(
    kcfg: KnowledgeConfig, per_type: int = 2, chunks_per_doc: int = 2
) -> list[str]:
    """Rewrite data/knowledge/samples/ from the current index (human review)."""
    from .citations import render_citation

    kcfg.samples_dir.mkdir(parents=True, exist_ok=True)
    index = Index(kcfg)
    try:
        by_type: dict[str, list] = {}
        for row in index.documents():
            by_type.setdefault(row["source_type"], []).append(row)
        written: list[str] = []
        for stype in sorted(by_type):
            lines: list[str] = [f"# samples: {stype}", ""]
            for row in by_type[stype][:per_type]:
                first_chunk = index.conn.execute(
                    "SELECT parent_id FROM chunks WHERE doc_id=? ORDER BY id LIMIT 1",
                    (row["doc_id"],),
                ).fetchone()
                parent = index.parent(first_chunk["parent_id"]) if first_chunk else None
                lines.append(f"## {render_citation(row, parent)}")
                lines.append(f"(file: {row['source_path']})")
                rows = index.conn.execute(
                    "SELECT text FROM chunks WHERE doc_id=? ORDER BY id LIMIT ?",
                    (row["doc_id"], chunks_per_doc),
                ).fetchall()
                for c in rows:
                    lines.append("")
                    lines.append(c["text"][:600])
                    lines.append("...")
                lines.append("")
            name = kcfg.samples_dir / f"{stype}.txt"
            name.write_text("\n".join(lines), encoding="utf-8")
            written.append(name.name)
        return written
    finally:
        index.close()


def _content_changed(row, path: Path) -> bool:
    try:
        return row["sha256"] != sha256_of(path)
    except Exception:
        return True


def sync(kcfg: KnowledgeConfig, force: bool = False) -> SyncReport:
    """Reconcile the index with the source folder. Never raises for
    per-file failures: those land in the report and the rest of the sync
    completes."""
    t0 = time.monotonic()
    report = SyncReport()
    files = source_files(kcfg.source_dir)
    index = Index(kcfg)
    try:
        # --- remove: indexed but no longer in the source folder ------------
        by_rel: dict[str, object] = {
            r["source_path"]: r for r in index.documents()
        }
        for rel in sorted(set(by_rel) - set(files)):
            index.delete_document(by_rel[rel]["doc_id"])  # type: ignore[index]
            report.removed.append(rel)
            log.info("removed from index: %s", rel)

        # --- to parse: new or content-changed -------------------------------
        todo: list[tuple[str, Path]] = []
        for rel, path in files.items():
            row = by_rel.get(rel)
            if row is None or force or _content_changed(row, path):
                todo.append((rel, path))
        if not todo:
            report.seconds = time.monotonic() - t0
            return report

        # --- parse (fail-loud per file; one bad file never stops the sync) --
        parsed_docs: list[ParsedDoc] = []
        for rel, path in todo:
            try:
                parsed_docs.append(parse_file(path, kcfg.source_dir, kcfg))
            except IngestError as e:
                report.failed.append((rel, str(e)))
                log.error("parse failed: %s: %s", rel, e)
            except Exception as e:
                report.failed.append((rel, f"{type(e).__name__}: {e}"))
                log.exception("parse crashed: %s", rel)

        # --- chunk + flag + embed (one batched embed pass) ------------------
        plan: list[tuple[ParsedDoc, list, list]] = []
        for parsed in parsed_docs:
            parents, chunks = chunk_markdown(
                parsed.doc_id, parsed.markdown, kcfg, parsed.ocr_suspect
            )
            if not chunks:
                report.failed.append((parsed.source_path, "no chunks produced"))
                log.error("no chunks: %s", parsed.source_path)
                continue
            plan.append((parsed, parents, chunks))
            if parsed.ocr_suspect:
                report.flagged.append(parsed.source_path)
                log.warning("flagged ocr-suspect: %s", parsed.source_path)

        if plan:
            texts = [c.text for _, _, chunks in plan for c in chunks]
            vectors = Embedder(kcfg).embed(texts)
            i = 0
            for parsed, parents, chunks in plan:
                n = len(chunks)
                index.upsert_document(parsed, parents, chunks, vectors[i:i + n])
                i += n
                report.parsed.append(parsed.source_path)

        report.parsed.sort()
        report.flagged.sort()
        report.seconds = time.monotonic() - t0
        log.info("sync done: %s", report.summary())
        try:
            dump_samples(kcfg)
        except Exception:
            log.exception("sample dump failed (index is fine)")
        return report
    finally:
        index.close()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    from config import AppConfig, ensure_dirs

    cfg = AppConfig()
    ensure_dirs(cfg)
    kcfg = KnowledgeConfig(
        root=cfg.knowledge_dir,
        source_dir=cfg.knowledge_source_dir,
        models_dir=cfg.models_dir,
        tessdata_prefix=cfg.models_dir / "tessdata",
    )
    report = sync(kcfg, force="--force" in sys.argv[1:])
    print(report.details())
    return 1 if report.failed else 0


if __name__ == "__main__":
    sys.exit(main())
