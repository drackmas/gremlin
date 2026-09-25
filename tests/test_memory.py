"""Persistent memory store and its model-facing tools."""

from __future__ import annotations

import json

from memory.store import MemoryStore
from tools import build_registry
from tools.memory import build_memory_tools


def test_store_crud_roundtrip(tmp_path):
    store = MemoryStore(tmp_path / "memory.json")
    mid = store.add("user likes tea", tags=["prefs"])
    assert store.get(mid)["content"] == "user likes tea"
    assert [m["id"] for m in store.search("tea")] == [mid]
    assert store.search("nope") == []
    # reload from disk
    store2 = MemoryStore(tmp_path / "memory.json")
    assert store2.get(mid)["content"] == "user likes tea"
    assert store2.remove(mid) is True
    assert store2.get(mid) is None
    assert store2.remove(mid) is False


def test_store_corrupt_file_starts_empty(tmp_path):
    p = tmp_path / "memory.json"
    p.write_text("{not json", encoding="utf-8")
    store = MemoryStore(p)
    assert store.pinned() == []
    assert store.search("anything") == []


def test_store_pin(tmp_path):
    store = MemoryStore(tmp_path / "memory.json")
    a = store.add("a")
    store.add("b")
    assert store.pinned() == []
    assert store.pin(a) is True
    assert [m["id"] for m in store.pinned()] == [a]
    assert store.pin(a, value=False) is True
    assert store.pinned() == []
    assert store.pin("missing") is False


def test_memory_tools_roundtrip(tmp_path):
    store = MemoryStore(tmp_path / "memory.json")
    tools = {t.name: t for t in build_memory_tools(store)}

    out = tools["memory_add"].handler({"content": "deploy on Fridays", "tags": ["ops"], "pin": True})
    mid = out.split()[-1]
    assert mid in tools["memory_search"].handler({"query": "fridays"})
    assert store.get(mid)["content"] == "deploy on Fridays"
    assert store.pinned()[0]["id"] == mid
    assert tools["memory_search"].handler({"query": "zzz"}) == "no matching memories"
    assert json.loads(tools["memory_get"].handler({"id": mid}))["id"] == mid
    assert tools["memory_pin"].handler({"id": mid, "value": False}) == f"memory {mid} pinned=False"
    assert store.pinned() == []
    assert tools["memory_remove"].handler({"id": mid}) == f"removed memory {mid}"
    assert tools["memory_get"].handler({"id": "nope"}) == "no memory with id nope"


def test_registry_includes_memory_tools(cfg):
    registry = build_registry(cfg)
    names = set(registry.names())
    assert {"memory_add", "memory_search", "memory_get", "memory_remove", "memory_pin"} <= names

