"""Skill discovery and loading.

Skills are instruction-only resources: ``skills/<slug>/SKILL.md`` with a
small frontmatter block (``name``, ``description``). They are never executed;
the model reads them (via the ``load_skill`` tool) as instructions on how to
accomplish a job with the available tools.

The cache is invalidated by the *content signature* (the sorted set of skill
slugs plus each file's mtime), so both in-place edits and new/removed skill
directories are detected without relying on directory-mtime semantics that
vary across filesystems. ``reload()`` forces a rebuild (used after the
``create_skill`` self-extension tool writes a new skill).
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

log = logging.getLogger("gremlin.skills")


class SkillNotFoundError(KeyError):
    pass


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Split a SKILL.md into (metadata, body). No external yaml dependency:
    the frontmatter is a ``---`` fenced block of simple ``key: value`` lines."""
    meta: dict[str, str] = {}
    body = text
    m = re.match(r"\A---\s*\n(.*?)\n---\s*\n?(.*)\Z", text, re.DOTALL)
    if m:
        for line in m.group(1).splitlines():
            if ":" in line:
                k, _, v = line.partition(":")
                meta[k.strip().lower()] = v.strip().strip("'\"")
        body = m.group(2)
    return meta, body


class SkillLoader:
    def __init__(self, cfg) -> None:
        self.dir = Path(cfg.skills_dir)
        # slug -> (name, description, full_text, mtime)
        self._cache: dict[str, tuple[str, str, str, float]] = {}
        self._signature: tuple[Any, ...] | None = None

    # --- signature / cache ---------------------------------------------
    def _scan(self) -> tuple[Any, ...]:
        """Identify the on-disk skill set: (slug, mtime) pairs, sorted."""
        if not self.dir.is_dir():
            return ()
        sig: list[tuple[str, float]] = []
        for sub in self.dir.iterdir():
            skill_file = sub / "SKILL.md"
            if not sub.is_dir() or not skill_file.is_file():
                continue
            try:
                sig.append((sub.name, skill_file.stat().st_mtime))
            except OSError:
                continue
        return tuple(sorted(sig))

    def _build(self) -> None:
        cache: dict[str, tuple[str, str, str, float]] = {}
        for sub in sorted(self.dir.iterdir(), key=lambda p: p.name) if self.dir.is_dir() else []:
            skill_file = sub / "SKILL.md"
            if not sub.is_dir() or not skill_file.is_file():
                continue
            try:
                text = skill_file.read_text(encoding="utf-8")
                meta, _ = parse_frontmatter(text)
                mtime = skill_file.stat().st_mtime
            except (OSError, UnicodeDecodeError) as e:
                log.warning("skipping unreadable skill %s: %s", sub.name, e)
                continue
            cache[sub.name] = (
                meta.get("name") or sub.name,
                meta.get("description", ""),
                text,
                mtime,
            )
        self._cache = cache
        self._signature = self._scan()

    def _ensure_cache(self) -> None:
        sig = self._scan()
        if self._signature is None or sig != self._signature:
            self._build()

    def reload(self) -> None:
        """Force a cache rebuild (call after writing a new/edited skill)."""
        self._build()

    # --- API -----------------------------------------------------------
    def list(self) -> list[dict]:
        """List available skills as ``{slug, name, description}`` dicts."""
        self._ensure_cache()
        return [
            {"slug": slug, "name": name, "description": desc}
            for slug, (name, desc, _, _) in sorted(self._cache.items())
        ]

    def get(self, name: str) -> str:
        """Full skill text by name or slug (case-insensitive)."""
        self._ensure_cache()
        want = (name or "").strip().lower()
        for slug, (skill_name, _, full_text, _) in self._cache.items():
            if want in (slug.lower(), skill_name.lower()):
                return full_text
        raise SkillNotFoundError(name)
