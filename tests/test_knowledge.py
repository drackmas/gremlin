"""Knowledge library tests: chunking, sync (fail-loud), search, tool gate."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from chat.settings import SettingsStore

from knowledge.chunking import chunk_markdown
from knowledge.config import KnowledgeConfig
from knowledge.embed import Embedder
from knowledge.search import search
from knowledge.store import Index
from knowledge.sync import sync

PROJECT_ROOT = Path(__file__).resolve().parent.parent
# Embedding weights are a machine-level cache (like piper voices): reuse the
# real models/ dir so tests do not re-download 90MB.
MODELS_DIR = PROJECT_ROOT / "models"


def kcfg(tmp_path: Path) -> KnowledgeConfig:
    return KnowledgeConfig(
        root=tmp_path / "data" / "knowledge",
        source_dir=tmp_path / "files",
        models_dir=MODELS_DIR,
    )


def write_source(tmp_path: Path, name: str, text: str) -> None:
    d = tmp_path / "files"
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(text, encoding="utf-8")


# ---------------------------------------------------------------- chunking --

MD = """# Book of Etiquette

Some opening words about manners.

## Chapter 1

The host arrives first and greets each guest by name.

## Chapter 2

Salt is passed handle first, and bread is never broken by the host.
"""


def test_chunks_respect_headings(tmp_path):
    # low min_chars so the small chapters stay split
    cfg = dataclasses.replace(kcfg(tmp_path), chunk_min_chars=10)
    parents, chunks = chunk_markdown("doc1", MD, cfg)
    assert len(parents) >= 2
    texts = [p.text for p in parents]
    assert any("Chapter 1" in t and "greet" in t for t in texts)
    assert any("Chapter 2" in t and "Salt" in t for t in texts)


def test_char_offsets_slice_source_exactly(tmp_path):
    parents, chunks = chunk_markdown("doc1", MD, kcfg(tmp_path))
    for p in parents:
        assert MD[p.char_start : p.char_end].strip() == p.text
    for c in chunks:
        assert MD[c.char_start : c.char_end] == c.text


def test_huge_unit_splitted_under_cap(tmp_path):
    cfg = kcfg(tmp_path)
    big = "# Title\n\n" + ("word " * 2000)
    parents, chunks = chunk_markdown("doc1", big, cfg)
    assert all(len(c.text) <= cfg.chunk_max_chars for c in chunks)
    assert len(chunks) > 1
    for c in chunks:
        assert big[c.char_start : c.char_end] == c.text


def test_timestamps_become_own_units(tmp_path):
    cfg = kcfg(tmp_path)
    md = "# Talk\n\n[00:00:12] First spoken line.\n\n[00:01:05] Second spoken line.\n"
    parents, _ = chunk_markdown("doc1", md, cfg)
    assert any(p.timestamp == "00:00:12" for p in parents)
    assert any(p.timestamp == "00:01:05" for p in parents)


# -------------------------------------------------------------------- sync --


def test_sync_reports_and_index(tmp_path):
    write_source(
        tmp_path,
        "manners-of-women-1908-ch3.txt",
        "The host arrives first and greets each guest by name.",
    )
    write_source(tmp_path, "broken.pdf", "not a real pdf")
    report = sync(kcfg(tmp_path))
    assert report.summary().startswith("1 parsed"), report.details()
    assert len(report.failed) == 1 and "broken.pdf" in report.failed[0][0]

    index = Index(kcfg(tmp_path))
    docs = index.documents()
    index.close()
    assert len(docs) == 1
    row = docs[0]
    # filename metadata: humanized title + year from the name
    assert "manners of women 1908" in row["title"]
    assert row["date"] == "1908"


def test_sync_meta_sidecar_overrides(tmp_path):
    write_source(tmp_path, "note.txt", "Plain text note.")
    (tmp_path / "files" / "note.txt.meta").write_text(
        "author: Ada\n"
        "date: 1890-05-01\n"
        "topic: letters\n",
        encoding="utf-8",
    )
    sync(kcfg(tmp_path))
    index = Index(kcfg(tmp_path))
    row = index.documents()[0]
    index.close()
    assert row["author"] == "Ada"
    assert row["date"] == "1890-05-01"
    assert "letters" in row["topic"]


def test_sync_add_then_remove(tmp_path):
    write_source(tmp_path, "a.txt", "Alpha content.")
    assert sync(kcfg(tmp_path)).summary().startswith("1 parsed")

    write_source(tmp_path, "b.txt", "Beta content.")
    report = sync(kcfg(tmp_path))
    assert report.summary().startswith("1 parsed")
    assert "b.txt" in report.parsed

    (tmp_path / "files" / "a.txt").unlink()
    report = sync(kcfg(tmp_path))
    assert "1 removed" in report.summary()
    index = Index(kcfg(tmp_path))
    docs = index.documents()
    index.close()
    assert [d["title"] for d in docs] == ["b"]


def test_sync_idempotent(tmp_path):
    write_source(tmp_path, "a.txt", "Alpha content.")
    sync(kcfg(tmp_path))
    report = sync(kcfg(tmp_path))
    assert report.summary().startswith("0 parsed")
    assert not report.failed


def test_sync_writes_samples(tmp_path):
    write_source(tmp_path, "a.txt", "Alpha " * 200)
    sync(kcfg(tmp_path))
    samples = (tmp_path / "data" / "knowledge" / "samples").glob("*.txt")
    assert any("txt" in s.name for s in samples)


def test_sync_unsupported_skipped_not_failed(tmp_path):
    write_source(tmp_path, "image.png", "not a png")
    report = sync(kcfg(tmp_path))
    assert not report.failed
    assert report.summary().startswith("0 parsed")


# ------------------------------------------------------------------ search --


@pytest.fixture(scope="module")
def app_cfg(tmp_path_factory):
    """App rooted in a shared temp tree, with two real source docs."""
    from config import AppConfig, ensure_dirs

    tmp = tmp_path_factory.mktemp("knowledge-seed")
    cfg = AppConfig(root=tmp)
    ensure_dirs(cfg)
    (cfg.knowledge_source_dir).mkdir(parents=True, exist_ok=True)
    (cfg.knowledge_source_dir / "manners-of-women-1908.txt").write_text(
        (
            "# Manners of Women\n\n"
            "The host arrives first and greets each guest by name. "
            "Salt is passed handle first, and the bread is never broken "
            "by the host. A gentleman yields the door to a lady.\n"
        ),
        encoding="utf-8",
    )
    (cfg.knowledge_source_dir / "db-plan-final.txt").write_text(
        (
            "# DB Plan Final\n\n"
            "The library is a disposable index: the source of truth is the "
            "owner's files. Benchmark sqlite-vec vs numpy brute force at "
            "250k chunks. Embeddings use all-MiniLM-L6-v2.\n"
        ),
        encoding="utf-8",
    )
    return cfg


@pytest.fixture(scope="module")
def seeded(app_cfg):
    """The synced KnowledgeConfig for the shared tree (one embed pass)."""
    kc = KnowledgeConfig(
        root=app_cfg.knowledge_dir,
        source_dir=app_cfg.knowledge_source_dir,
        models_dir=MODELS_DIR,
    )
    report = sync(kc)
    assert report.summary().startswith("2 parsed"), report.details()
    return kc


def test_search_ranks_expected_doc(seeded):
    index = Index(seeded)
    hits = search(
        seeded, index, "which embedding model does the plan use",
        k=3, embedder=Embedder(seeded),
    )
    index.close()
    assert hits
    assert "db plan" in hits[0].title.lower()


def test_search_no_match_returns_empty(seeded):
    index = Index(seeded)
    hits = search(
        seeded, index, "quantum chromodynamics quark confinement",
        k=3, embedder=Embedder(seeded),
    )
    index.close()
    assert isinstance(hits, list)


def test_tool_gate_follows_setting(app_cfg):
    from tools import build_registry

    store = SettingsStore(app_cfg)
    store.save({"knowledge_enabled": False})
    registry_off = build_registry(app_cfg, None, None, lambda: store.load())
    assert "search_knowledge" not in registry_off.names()

    store.save({"knowledge_enabled": True})
    registry_on = build_registry(app_cfg, None, None, lambda: store.load())
    assert "search_knowledge" in registry_on.names()

    result, ok = registry_on.execute(
        "search_knowledge", {"query": "greeting each guest by name", "k": 2}
    )
    assert ok
    assert "manners" in result.lower()


def test_tool_off_returns_error(app_cfg):
    from tools import build_registry

    SettingsStore(app_cfg).save({"knowledge_enabled": False})
    registry = build_registry(app_cfg, None, None, lambda: SettingsStore(app_cfg).load())
    result, ok = registry.execute("search_knowledge", {"query": "anything"})
    assert not ok
    assert "disabled" in result


def test_tool_default_k_from_setting(app_cfg, seeded):
    from tools import build_registry

    store = SettingsStore(app_cfg)
    store.save({"knowledge_enabled": True, "knowledge_k": 1})
    registry = build_registry(app_cfg, None, None, lambda: store.load())
    result, ok = registry.execute(
        "search_knowledge", {"query": "disposable index embeddings"}
    )
    assert ok
    # the setting (not the built-in default of 5) limits the hits
    assert "[1]" in result
    assert "[2]" not in result

    store.save({"knowledge_k": 2})
    result, ok = registry.execute(
        "search_knowledge", {"query": "disposable index embeddings"}
    )
    assert ok
    # both search legs rank both docs, so k=2 surfaces the second hit
    assert "[2]" in result
