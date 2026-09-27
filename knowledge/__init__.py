"""Knowledge library: local, CPU-only, single-file index over the owner's files.

Source of truth = files in the configured source folder (``library/``). The
index under ``data/knowledge/`` is disposable: rebuildable from scratch with
zero loss. See ``knowledge/config.py`` for every tunable.

Modules:
  survey    -- scan the source folders, report what is parseable
  ingest    -- parse source files to markdown (fail-loud)
  chunking  -- structure-aware chunking
  embedding -- local ONNX embeddings
  store     -- vector store (sqlite-vec)
  bm25      -- lexical search
  search    -- hybrid RRF search + filters + budgets
  citations -- per-source-type citation rendering
  tool      -- Gremlin tool: search_knowledge
  sync      -- on-purpose sync pipeline (python -m knowledge.sync)
  eval      -- recall@k evaluation
"""
