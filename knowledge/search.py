"""Hybrid search: FTS5 BM25 + sqlite-vec, merged via reciprocal rank fusion.

Both legs run independently, each keeps ``candidate_n`` results, metadata
filters are applied per leg, then scores fuse:

    score(c) = w_bm25 / (rrf_k + rank_bm25) + w_vec / (rrf_k + rank_vec)

Starting weights are 1:1 (the plan); tune only against the eval set.
``k`` defaults to ``default_k`` and is hard-capped at ``max_k`` — never
unbounded. An empty result is a normal result, not an error.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass

import numpy as np

from .bm25 import bm25_search
from .citations import render_citation
from .config import KnowledgeConfig
from .embed import Embedder
from .store import Index

W_BM25 = 1.0
W_VEC = 1.0


@dataclass
class Hit:
    rank: int
    chunk_id: str
    doc_id: str
    title: str
    source_type: str
    text: str
    context: str
    page: int | None
    timestamp: str | None
    heading: str | None
    char_start: int
    char_end: int
    ocr_suspect: bool
    score: float
    citation: str


def _filter_match(doc: sqlite3.Row, *, source_type: str | None, topic: str | None,
                  author: str | None, date_from: str | None,
                  date_to: str | None) -> bool:
    if source_type and doc["source_type"] != source_type:
        return False
    if topic and topic.lower() not in (doc["topic"] or "").lower():
        return False
    if author and author.lower() not in (doc["author"] or "").lower():
        return False
    date = doc["date"] or ""
    if date_from and date and date < date_from:
        return False
    if date_to and date and date > date_to:
        return False
    return True


def _vector_search(conn: sqlite3.Connection, embedding: np.ndarray,
                   k: int) -> list[tuple[str, float]]:
    try:
        rows = conn.execute(
            """SELECT chunk_id, distance
               FROM chunks_vec
               WHERE embedding MATCH ? AND k = ?
               ORDER BY distance""",
            (embedding.astype("<f4").tobytes(), k),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [(r["chunk_id"], r["distance"]) for r in rows]


def _context(parent_text: str, chunk_text: str, chunk_start: int,
             parent_start: int, budget: int) -> str:
    """Parent context capped at ``budget`` chars, centered on the chunk."""
    if len(parent_text) <= budget:
        return parent_text
    local = max(0, chunk_start - parent_start)
    start = max(0, local - budget // 2)
    end = min(len(parent_text), start + budget)
    start = max(0, end - budget)
    out = parent_text[start:end]
    if start > 0:
        out = "…" + out
    if end < len(parent_text):
        out = out + "…"
    return out


def search(
    cfg: KnowledgeConfig,
    index: Index,
    query: str,
    *,
    source_type: str | None = None,
    topic: str | None = None,
    author: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    k: int | None = None,
    embedder: Embedder | None = None,
) -> list[Hit]:
    """Hybrid search; returns up to ``k`` complete hits (never truncated)."""
    k = cfg.default_k if k is None else max(1, min(int(k), cfg.max_k))
    query = (query or "").strip()
    if not query:
        return []
    conn = index.conn
    docs: dict[str, sqlite3.Row] = {}

    def doc_for(chunk_id: str) -> sqlite3.Row | None:
        row = conn.execute(
            """SELECT d.* FROM documents d
               JOIN chunks c ON c.doc_id = d.doc_id
               WHERE c.chunk_id = ?""",
            (chunk_id,),
        ).fetchone()
        if row is None:
            return None
        docs[row["doc_id"]] = row
        return row

    bm25_rows = bm25_search(conn, query, cfg.candidate_n)
    embedder = embedder or Embedder(cfg)
    vec = embedder.embed_one(query)
    vec_rows = _vector_search(conn, vec, cfg.candidate_n)

    scores: dict[str, float] = {}
    for rank, (cid, _s) in enumerate(bm25_rows, start=1):
        doc = doc_for(cid)
        if doc is None:
            continue
        if not _filter_match(doc, source_type=source_type, topic=topic,
                             author=author, date_from=date_from, date_to=date_to):
            continue
        scores[cid] = scores.get(cid, 0.0) + W_BM25 / (cfg.rrf_k + rank)
    for rank, (cid, _d) in enumerate(vec_rows, start=1):
        doc = doc_for(cid)
        if doc is None:
            continue
        if not _filter_match(doc, source_type=source_type, topic=topic,
                             author=author, date_from=date_from, date_to=date_to):
            continue
        scores[cid] = scores.get(cid, 0.0) + W_VEC / (cfg.rrf_k + rank)

    top = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)[:k]

    hits: list[Hit] = []
    for rank, (cid, score) in enumerate(top, start=1):
        chunk = index.chunk(cid)
        if chunk is None:
            continue
        parent = index.parent(chunk["parent_id"])
        doc = docs[chunk["doc_id"]]
        budget = cfg.parent_context_budget_chars
        context = _context(
            parent["text"] if parent else chunk["text"],
            chunk["text"],
            chunk["char_start"],
            parent["char_start"] if parent else chunk["char_start"],
            budget,
        )
        hits.append(
            Hit(
                rank=rank,
                chunk_id=cid,
                doc_id=doc["doc_id"],
                title=doc["title"],
                source_type=doc["source_type"],
                text=chunk["text"],
                context=context,
                page=parent["page"] if parent else None,
                timestamp=parent["timestamp"] if parent else None,
                heading=parent["heading"] if parent else None,
                char_start=chunk["char_start"],
                char_end=chunk["char_end"],
                ocr_suspect=bool(chunk["ocr_suspect"]),
                score=score,
                citation=render_citation(doc, parent),
            )
        )
    return hits
