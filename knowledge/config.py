"""Knowledge base configuration: every tunable lives here.

The library is a disposable index: the source of truth is the owner's files
under ``source_dir``. Everything under ``root`` can be deleted and rebuilt
from the source files with zero loss.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class KnowledgeConfig:
    root: Path          # data/knowledge (disposable index)
    source_dir: Path    # library/ (dedicated source folder; owner drops files here)
    models_dir: Path    # models/ (embedding weights cache)

    # --- chunking ---------------------------------------------------------
    chunk_max_chars: int = 1200    # hard cap for one chunk
    chunk_overlap_chars: int = 150  # overlap carried into the next chunk
    chunk_min_chars: int = 150     # merge chunks smaller than this

    # --- search / budgets ---------------------------------------------------
    parent_context_budget_chars: int = 800  # context shown around each hit
    result_max_chars: int = 8000            # total tool-result character budget
    rrf_k: int = 60                         # reciprocal-rank-fusion constant
    default_k: int = 5                      # default hits per query
    max_k: int = 20                         # hard cap on k
    candidate_n: int = 50                   # top-n kept from each search leg

    # --- embedding ------------------------------------------------------------
    embed_model: str = "sentence-transformers/all-MiniLM-L6-v2"
    embed_batch_size: int = 64

    # --- OCR fallback (extract-first, OCR only when extraction fails) ---------
    ocr_lang: str = "eng"
    ocr_dpi: int = 200
    ocr_min_text_chars: int = 40  # below this, a page is image-only -> OCR
    tessdata_prefix: Path | None = None  # TESSDATA_PREFIX for tesseract; None = system default

    # --- audio / video transcription (faster-whisper, local, CPU) --------------
    transcribe_model: str = "small"
    transcribe_language: str = "en"

    # --- derived paths ----------------------------------------------------------
    @property
    def markdown_dir(self) -> Path:
        """Parsed-markdown cache, keyed by content hash (disposable)."""
        return self.root / "markdown"

    @property
    def samples_dir(self) -> Path:
        """Sample chunk dump for human review before trusting the index."""
        return self.root / "samples"

    @property
    def eval_dir(self) -> Path:
        """Eval questions + recall@k results."""
        return self.root / "eval"

    @property
    def index_path(self) -> Path:
        """Single-file SQLite index (inspectable with the sqlite3 CLI)."""
        return self.root / "index.db"

    @property
    def embed_cache_dir(self) -> Path:
        return self.models_dir / "embed"
