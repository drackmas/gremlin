"""Gremlin tool: search_knowledge.

Registered like any other tool but gated: ``visible`` checks the live
settings on every call, so the KNOWLEDGE toggle takes effect immediately
(no restart) both in the tool list sent to the model and at dispatch.
"""
from __future__ import annotations

from config import AppConfig
from tools.registry import Tool

from .config import KnowledgeConfig
from .embed import Embedder
from .search import search
from .store import Index

_DESCRIPTION = """\
Search the owner's personal knowledge library (books, transcripts, documents,
transcribed audio/video) and retrieve relevant passages with citations.

Use it for factual questions about the owner's files: quotes, names, dates,
plans, testimony, video/audio content. Returns ranked hits, each with the
matched passage, budgeted parent context, and a clean citation. Filters:
source_type (transcript|txt|pdf|epub|docx|doc|audio|video), topic, author,
date_from/date_to (YYYY-MM-DD or YYYY). An empty result is a normal result:
retry with a shorter or differently-worded query, or conclude the library
does not cover it. This tool retrieves and cites; it does not verify content.
"""

_SOURCE_TYPES = [
    "transcript", "txt", "pdf", "epub", "docx", "doc", "audio", "video",
]


def _render_hits(hits, cfg: KnowledgeConfig) -> str:
    """Render hits; drop lowest-ranked COMPLETE hits to fit the budget
    (never cut a hit in half)."""
    blocks: list[str] = []
    for h in hits:
        block = [f"[{h.rank}] {h.citation}"]
        block.append(f"passage: {h.text}")
        if h.context and h.context != h.text:
            block.append(f"context: {h.context}")
        if h.ocr_suspect:
            block.append("warning: OCR-suspect passage (may contain recognition errors)")
        blocks.append("\n".join(block))
    out: list[str] = []
    total = 0
    for i, b in enumerate(blocks):
        if out and total + len(b) + 1 > cfg.result_max_chars:
            return "\n\n".join(out) + f"\n\n({len(blocks) - len(out)} lower-ranked hit(s) omitted to fit the result budget)"
        out.append(b)
        total += len(b) + 1
    return "\n\n".join(out) if out else ""


def build_knowledge_tool(cfg: AppConfig, settings_loader) -> Tool:
    """Build the (settings-gated) search_knowledge tool."""
    kcfg = KnowledgeConfig(
        root=cfg.knowledge_dir,
        source_dir=cfg.knowledge_source_dir,
        models_dir=cfg.models_dir,
    )

    def visible() -> bool:
        try:
            return bool(settings_loader().get("knowledge_enabled", False))
        except Exception:
            return False

    def handler(args: dict) -> str:
        if not visible():
            return (
                "ERROR: the knowledge base is disabled. Tell the owner to turn "
                "on the KNOWLEDGE toggle in settings; do not retry this tool."
            )
        try:
            index = Index(kcfg)
        except Exception as e:
            return f"ERROR: knowledge index unavailable: {e}"
        try:
            hits = search(
                kcfg,
                index,
                args.get("query", ""),
                source_type=args.get("source_type"),
                topic=args.get("topic"),
                author=args.get("author"),
                date_from=args.get("date_from"),
                date_to=args.get("date_to"),
                k=args.get("k"),
                embedder=Embedder(kcfg),
            )
        except Exception as e:
            return f"ERROR: knowledge search failed: {e}"
        finally:
            index.close()
        if not hits:
            return (
                "No passages matched in the knowledge library. Try a shorter "
                "query with fewer terms, different wording, or a broader "
                "source_type filter."
            )
        return _render_hits(hits, kcfg)

    return Tool(
        name="search_knowledge",
        description=_DESCRIPTION,
        parameters={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Natural-language query (a few words to a sentence).",
                },
                "source_type": {
                    "type": "string",
                    "enum": _SOURCE_TYPES,
                    "description": "Restrict to one source type.",
                },
                "topic": {
                    "type": "string",
                    "description": "Free-form topic tag the document was tagged with.",
                },
                "author": {
                    "type": "string",
                    "description": "Author/channel substring.",
                },
                "date_from": {
                    "type": "string",
                    "description": "Only documents dated on/after this (YYYY or YYYY-MM-DD).",
                },
                "date_to": {
                    "type": "string",
                    "description": "Only documents dated on/before this (YYYY or YYYY-MM-DD).",
                },
                "k": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 20,
                    "description": "Number of hits to return (default 5, max 20).",
                },
            },
            "required": ["query"],
        },
        handler=handler,
        visible=visible,
    )
