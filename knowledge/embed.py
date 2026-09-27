"""Local embedding: fastembed (ONNX, CPU) behind a tiny swappable interface.

Model: all-MiniLM-L6-v2, 384-dim. Weights live in ``models/embed`` (or the
HF cache). Vectors are L2-normalized so cosine == dot product.
"""
from __future__ import annotations

import logging

import numpy as np

from .config import KnowledgeConfig

log = logging.getLogger("gremlin.knowledge.embed")


class Embedder:
    """Lazy, batched embedder. ``dim`` is known only after first use."""

    def __init__(self, cfg: KnowledgeConfig) -> None:
        self.cfg = cfg
        self._model = None
        self._dim: int | None = None

    def _ensure(self):
        if self._model is None:
            from fastembed import TextEmbedding

            log.info("loading embedder %s (cache: %s)",
                     self.cfg.embed_model, self.cfg.embed_cache_dir)
            self._model = TextEmbedding(
                model_name=self.cfg.embed_model,
                cache_dir=str(self.cfg.embed_cache_dir),
            )
            try:
                self._dim = int(self._model.get_embedding_size(self.cfg.embed_model))
            except Exception:
                self._dim = len(next(self._model.embed(["probe"])))
        return self._model

    @property
    def dim(self) -> int:
        self._ensure()
        return self._dim  # type: ignore[return-value]

    def embed(self, texts: list[str]) -> np.ndarray:
        """Embed a list of texts -> (n, dim) float32, L2-normalized rows."""
        model = self._ensure()
        if not texts:
            return np.zeros((0, self.dim), dtype="float32")
        out: list[np.ndarray] = []
        bs = max(1, self.cfg.embed_batch_size)
        for i in range(0, len(texts), bs):
            batch = texts[i:i + bs]
            out.extend(np.asarray(v, dtype="float32") for v in model.embed(batch))
        arr = np.vstack(out).astype("float32")
        norms = np.linalg.norm(arr, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        return arr / norms

    def embed_one(self, text: str) -> np.ndarray:
        return self.embed([text])[0]
