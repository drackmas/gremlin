"""Persistent memory: a small JSON list of notes the model can grow.

Pinned memories are injected into the system prompt every turn (see
``chat.prompts.build_system_prompt``); everything else is reachable through
the memory tools in ``tools/memory.py``.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from pathlib import Path

log = logging.getLogger("gremlin.memory")


class MemoryStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._items: list[dict] = []
        self.load()

    # --- persistence ----------------------------------------------------
    def load(self) -> None:
        if not self.path.exists():
            self._items = []
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, list):
                raise ValueError("expected a list of memories")
            self._items = [m for m in data if isinstance(m, dict) and m.get("content")]
        except (json.JSONDecodeError, OSError, ValueError) as e:
            log.warning("memory file %s unreadable (%s); starting empty", self.path, e)
            self._items = []

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._items, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)

    # --- CRUD -------------------------------------------------------------
    def add(self, content: str, tags: list[str] | None = None, pin: bool = False) -> str:
        content = (content or "").strip()
        if not content:
            raise ValueError("memory content must be non-empty")
        memory_id = uuid.uuid4().hex[:8]
        self._items.append(
            {
                "id": memory_id,
                "content": content,
                "tags": [t.strip() for t in (tags or []) if t.strip()],
                "pinned": bool(pin),
            }
        )
        self.save()
        return memory_id

    def get(self, memory_id: str) -> dict | None:
        for m in self._items:
            if m.get("id") == memory_id:
                return m
        return None

    def search(self, query: str, in_content: bool = True) -> list[dict]:
        q = (query or "").strip().lower()
        if not q:
            return []
        out = []
        for m in self._items:
            in_tags = any(q in t.lower() for t in m.get("tags", []))
            in_text = in_content and q in m.get("content", "").lower()
            if in_tags or in_text:
                out.append(m)
        return out

    def remove(self, memory_id: str) -> bool:
        before = len(self._items)
        self._items = [m for m in self._items if m.get("id") != memory_id]
        if len(self._items) != before:
            self.save()
            return True
        return False

    def pin(self, memory_id: str, value: bool = True) -> bool:
        m = self.get(memory_id)
        if m is None:
            return False
        m["pinned"] = bool(value)
        self.save()
        return True

    def pinned(self) -> list[dict]:
        return [m for m in self._items if m.get("pinned")]
