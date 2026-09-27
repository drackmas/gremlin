"""Store benchmark: sqlite-vec vs numpy brute force at plan scale.

The plan (db-plan-final.txt): "benchmark sqlite-vec vs numpy brute force
AT 250k and decide there." This script generates a synthetic corpus of
~250k characters, embeds it with the real model, then measures build
time, top-k query latency (median of 100 queries), and storage size for
both stores.

Usage: python -m knowledge.benchmark [target_chars]
"""
from __future__ import annotations

import random
import statistics
import sys
import tempfile
import time
from pathlib import Path

import numpy as np

from .chunking import Chunk, Parent
from .config import KnowledgeConfig
from .embed import Embedder
from .ingest.models import DocumentMeta, ParsedDoc
from .store import Index

_WORDS = (
    "the owner's knowledge base index documents transcripts videos books "
    "etiquette bible changes mandela effect prophecy testimony prayer faith "
    "chunk parent heading page timestamp citation search retrieval ranking "
    "vector embedding cosine distance reciprocal rank fusion sqlite numpy "
    "benchmark latency memory disk build insert query candidate filter "
).split()


def _synthetic_markdown(total_chars: int, chunk_chars: int = 1200) -> list[str]:
    """Random-word paragraphs summing to ~total_chars (real embedding spread)."""
    rng = random.Random(42)
    texts: list[str] = []
    made = 0
    while made < total_chars:
        words = [rng.choice(_WORDS) for _ in range(max(20, chunk_chars // 6))]
        t = " ".join(words)
        texts.append(t)
        made += len(t)
    return texts


def _bench_numpy(vecs: np.ndarray, queries: np.ndarray, k: int) -> tuple[float, float]:
    """Returns (build_s, median_query_ms)."""
    t0 = time.monotonic()
    store = vecs.copy()
    build = time.monotonic() - t0
    times = []
    for q in queries:
        t0 = time.monotonic()
        np.argpartition(-(store @ q), k - 1)
        times.append((time.monotonic() - t0) * 1000)
    return build, statistics.median(times)


def _bench_sqlite_vec(
    kcfg: KnowledgeConfig, vecs: np.ndarray, queries: np.ndarray, k: int
) -> tuple[float, float, float]:
    """Returns (build_s, median_query_ms, db_size_mb)."""
    index = Index(kcfg)
    try:
        n = len(vecs)
        doc = ParsedDoc(
            doc_id="bench",
            source_path="bench.txt",
            source_type="txt",
            markdown="",
            sha256="0" * 64,
            meta=DocumentMeta(title="bench"),
            size_bytes=0,
        )
        # one parent holding all chunks (benchmark only cares about vectors)
        parents = [
            Parent(parent_id="bench-p0", doc_id="bench", text="bench",
                   char_start=0, char_end=0)
        ]
        chunks = [
            Chunk(
                chunk_id=f"bench-c{i}", parent_id="bench-p0", doc_id="bench",
                text=f"chunk {i}", char_start=0, char_end=0,
            )
            for i in range(n)
        ]
        t0 = time.monotonic()
        index.upsert_document(doc, parents, chunks, vecs)
        build = time.monotonic() - t0
        times = []
        for q in queries:
            t0 = time.monotonic()
            index.conn.execute(
                "SELECT chunk_id, distance FROM chunks_vec "
                "WHERE embedding MATCH ? AND k = ? ORDER BY distance",
                (q.astype("<f4").tobytes(), k),
            ).fetchall()
            times.append((time.monotonic() - t0) * 1000)
        size_mb = kcfg.index_path.stat().st_size / 1e6
        return build, statistics.median(times), size_mb
    finally:
        index.close()


def main(target_chars: int = 250_000) -> int:
    import logging

    logging.basicConfig(level=logging.WARNING)

    tmp = Path(tempfile.mkdtemp(prefix="gremlin-bench-"))
    tmp.mkdir(parents=True, exist_ok=True)
    kcfg = KnowledgeConfig(
        root=tmp / "root", source_dir=tmp, models_dir=Path("models")
    )
    emb = Embedder(kcfg)

    texts = _synthetic_markdown(target_chars)
    total = sum(len(t) for t in texts)
    print(f"corpus: {len(texts)} chunks, {total} chars, model {kcfg.embed_model}")
    t0 = time.monotonic()
    vecs = emb.embed(texts)
    print(f"embedded {len(texts)} vectors ({vecs.shape[1]}-dim) in "
          f"{time.monotonic() - t0:.1f}s")

    rng = np.random.default_rng(7)
    queries = rng.normal(size=(100, vecs.shape[1])).astype("float32")
    queries /= np.linalg.norm(queries, axis=1, keepdims=True)
    k = 50

    b_np, q_np = _bench_numpy(vecs, queries, k)
    b_sv, q_sv, size_sv = _bench_sqlite_vec(kcfg, vecs, queries, k)

    mem_np_mb = vecs.nbytes / 1e6
    print()
    print(f"{'':18s} {'build':>10s} {'top-k median':>14s} {'storage':>12s}")
    print(f"{'numpy':18s} {b_np:9.3f}s {q_np:12.3f}ms {mem_np_mb:10.1f}MB RAM")
    print(f"{'sqlite-vec':18s} {b_sv:9.3f}s {q_sv:12.3f}ms {size_sv:10.1f}MB disk")
    print()
    print(f"k={k} | 100 queries timed | corpus ~250k chars (plan target)")
    return 0


if __name__ == "__main__":
    sys.exit(main(int(sys.argv[1]) if len(sys.argv) > 1 else 250_000))
