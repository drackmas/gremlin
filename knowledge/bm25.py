"""BM25 leg: FTS5 full-text search over chunks.

The raw query is sanitized into quoted terms joined by implicit AND, so any
user string (proper nouns, quoted phrases, punctuation) is safe to run and
still matches. bm25() scores are negative; ascending order = best first.
"""
from __future__ import annotations

import re

import sqlite3

_TERM_RE = re.compile(r"[\w]+(?:[-'\u2019][\w]+)*", re.UNICODE)


def fts_query(raw: str) -> str:
    """Sanitize a raw query into an FTS5 MATCH string (quoted terms, AND)."""
    terms: list[str] = []
    for t in _TERM_RE.findall(raw):
        t = t.strip()
        if t and t not in terms:
            terms.append(t)
    if not terms:
        return ""
    return " ".join(f'"{t.replace(chr(34), chr(34) * 2)}"' for t in terms)


def bm25_search(
    conn: sqlite3.Connection, query: str, limit: int
) -> list[tuple[str, float]]:
    """Top-``limit`` (chunk_id, bm25_score) for *query*; [] when no match."""
    q = fts_query(query)
    if not q:
        return []
    try:
        rows = conn.execute(
            """SELECT chunk_id, bm25(chunks_fts) AS s
               FROM chunks_fts
               WHERE chunks_fts MATCH ?
               ORDER BY s
               LIMIT ?""",
            (q, limit),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [(r["chunk_id"], r["s"]) for r in rows]
