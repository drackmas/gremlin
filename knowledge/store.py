"""Single-file SQLite index: documents + parents + chunks + FTS5 + sqlite-vec.

Inspectable with the sqlite3 CLI; the whole thing is disposable (rebuildable
from the markdown cache). The documents table doubles as the sync manifest
(sha256 + mtime + source path per file).

If the embedding model ever changes dimensionality, delete index.db and
re-sync: vec0 stores a fixed dimension at table-creation time.
"""
from __future__ import annotations

import logging
import sqlite3
import time

import numpy as np

from .config import KnowledgeConfig
from .ingest.models import ParsedDoc

log = logging.getLogger("gremlin.knowledge.store")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
  doc_id TEXT PRIMARY KEY,
  source_path TEXT NOT NULL,
  source_type TEXT NOT NULL,
  title TEXT NOT NULL,
  author TEXT NOT NULL DEFAULT '',
  date TEXT NOT NULL DEFAULT '',
  topic TEXT NOT NULL DEFAULT '',
  url TEXT NOT NULL DEFAULT '',
  sha256 TEXT NOT NULL,
  mtime REAL NOT NULL DEFAULT 0,
  size_bytes INTEGER NOT NULL DEFAULT 0,
  page_count INTEGER NOT NULL DEFAULT 0,
  duration_sec REAL NOT NULL DEFAULT 0,
  ocr_suspect INTEGER NOT NULL DEFAULT 0,
  warnings TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_documents_path ON documents(source_path);
CREATE INDEX IF NOT EXISTS idx_documents_type ON documents(source_type);
CREATE INDEX IF NOT EXISTS idx_documents_date ON documents(date);

CREATE TABLE IF NOT EXISTS parents (
  id INTEGER PRIMARY KEY,
  parent_id TEXT NOT NULL UNIQUE,
  doc_id TEXT NOT NULL,
  text TEXT NOT NULL,
  page INTEGER,
  timestamp TEXT,
  heading TEXT,
  char_start INTEGER NOT NULL,
  char_end INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_parents_doc ON parents(doc_id);

CREATE TABLE IF NOT EXISTS chunks (
  id INTEGER PRIMARY KEY,
  chunk_id TEXT NOT NULL UNIQUE,
  parent_id TEXT NOT NULL,
  doc_id TEXT NOT NULL,
  text TEXT NOT NULL,
  char_start INTEGER NOT NULL,
  char_end INTEGER NOT NULL,
  ocr_suspect INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_chunks_doc ON chunks(doc_id);
CREATE INDEX IF NOT EXISTS idx_chunks_parent ON chunks(parent_id);

CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
  text, content='chunks', content_rowid='id', tokenize='unicode61'
);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_vec USING vec0(
  chunk_id TEXT PRIMARY KEY,
  embedding FLOAT[384]
);
"""


class StoreError(RuntimeError):
    """Index-level failure (corrupt db, dim mismatch, missing sqlite-vec)."""


class Index:
    """Thin, explicit interface over index.db (the "store" the plan names)."""

    def __init__(self, cfg: KnowledgeConfig) -> None:
        self.cfg = cfg
        cfg.root.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(cfg.index_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        try:
            import sqlite_vec

            self.conn.enable_load_extension(True)
            sqlite_vec.load(self.conn)
            self.conn.enable_load_extension(False)
        except Exception as e:
            self.close()
            raise StoreError(f"sqlite-vec unavailable: {e}") from e
        self.conn.executescript(_SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        try:
            self.conn.commit()
            self.conn.close()
        except sqlite3.Error:
            pass

    # --- manifest ---------------------------------------------------------
    def documents(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM documents").fetchall()

    # --- upsert / delete ----------------------------------------------------
    def upsert_document(
        self,
        parsed: ParsedDoc,
        parents: list,
        chunks: list,
        embeddings: np.ndarray,
    ) -> None:
        """Replace one document's rows (documents + parents + chunks + fts + vec)."""
        doc_id = parsed.doc_id
        meta = parsed.meta
        if len(embeddings) != len(chunks):
            raise StoreError(
                f"{doc_id}: {len(embeddings)} embeddings for {len(chunks)} chunks"
            )
        conn = self.conn
        with conn:
            old = conn.execute(
                "SELECT id, text, chunk_id FROM chunks WHERE doc_id=?", (doc_id,)
            ).fetchall()
            for r in old:
                conn.execute(
                    "INSERT INTO chunks_fts(chunks_fts, rowid, text) "
                    "VALUES ('delete', ?, ?)",
                    (r["id"], r["text"]),
                )
                conn.execute(
                    "DELETE FROM chunks_vec WHERE chunk_id=?", (r["chunk_id"],)
                )
            conn.execute("DELETE FROM chunks WHERE doc_id=?", (doc_id,))
            conn.execute("DELETE FROM parents WHERE doc_id=?", (doc_id,))
            conn.execute("DELETE FROM documents WHERE doc_id=?", (doc_id,))
            conn.execute(
                """INSERT INTO documents (
                    doc_id, source_path, source_type, title, author, date,
                    topic, url, sha256, mtime, size_bytes, page_count,
                    duration_sec, ocr_suspect, warnings
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    doc_id,
                    parsed.source_path,
                    parsed.source_type,
                    meta.title,
                    meta.author,
                    meta.date,
                    meta.topic,
                    meta.extra.get("url", ""),
                    parsed.sha256,
                    time.time(),
                    parsed.size_bytes,
                    parsed.page_count,
                    parsed.duration_sec,
                    1 if parsed.ocr_suspect else 0,
                    "; ".join(parsed.warnings),
                ),
            )
            conn.executemany(
                """INSERT INTO parents (
                    parent_id, doc_id, text, page, timestamp, heading,
                    char_start, char_end
                ) VALUES (?,?,?,?,?,?,?,?)""",
                [
                    (
                        p.parent_id, p.doc_id, p.text, p.page, p.timestamp,
                        p.heading, p.char_start, p.char_end,
                    )
                    for p in parents
                ],
            )
            for chunk, vec in zip(chunks, embeddings):
                conn.execute(
                    """INSERT INTO chunks (
                        chunk_id, parent_id, doc_id, text, char_start,
                        char_end, ocr_suspect
                    ) VALUES (?,?,?,?,?,?,?)""",
                    (
                        chunk.chunk_id, chunk.parent_id, chunk.doc_id,
                        chunk.text, chunk.char_start, chunk.char_end,
                        1 if chunk.ocr_suspect else 0,
                    ),
                )
                rowid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                conn.execute(
                    "INSERT INTO chunks_fts(rowid, text) VALUES (?,?)",
                    (rowid, chunk.text),
                )
                conn.execute(
                    "INSERT INTO chunks_vec(chunk_id, embedding) VALUES (?,?)",
                    (chunk.chunk_id, vec.astype("<f4").tobytes()),
                )

    def delete_document(self, doc_id: str) -> None:
        conn = self.conn
        with conn:
            old = conn.execute(
                "SELECT id, text, chunk_id FROM chunks WHERE doc_id=?", (doc_id,)
            ).fetchall()
            for r in old:
                conn.execute(
                    "INSERT INTO chunks_fts(chunks_fts, rowid, text) "
                    "VALUES ('delete', ?, ?)",
                    (r["id"], r["text"]),
                )
                conn.execute(
                    "DELETE FROM chunks_vec WHERE chunk_id=?", (r["chunk_id"],)
                )
            conn.execute("DELETE FROM chunks WHERE doc_id=?", (doc_id,))
            conn.execute("DELETE FROM parents WHERE doc_id=?", (doc_id,))
            conn.execute("DELETE FROM documents WHERE doc_id=?", (doc_id,))

    # --- lookup helpers -------------------------------------------------------
    def chunk(self, chunk_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM chunks WHERE chunk_id=?", (chunk_id,)
        ).fetchone()

    def parent(self, parent_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM parents WHERE parent_id=?", (parent_id,)
        ).fetchone()

    def document(self, doc_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM documents WHERE doc_id=?", (doc_id,)
        ).fetchone()

    def stats(self) -> dict:
        out = {}
        for table in ("documents", "parents", "chunks"):
            out[table] = self.conn.execute(
                f"SELECT COUNT(*) n FROM {table}"
            ).fetchone()["n"]
        out["chars"] = self.conn.execute(
            "SELECT COALESCE(SUM(LENGTH(text)), 0) n FROM chunks"
        ).fetchone()["n"]
        return out
