"""Skill discovery and loading.

Skills are instruction-only resources: ``skills/<slug>/SKILL.md`` with a
small frontmatter block (``name``, ``description``). They are never executed;
the model reads them (via the ``load_skill`` tool) as instructions on how to
accomplish a job with the available tools.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

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
        self._cache: dict[str, tuple[str, str, str, float]] = {}
        self._dir_mtime: float | None = None

    def _ensure_cache(self) -> None:
        if self._cache and self._dir_mtime is not None:
            try:
                if self.dir.stat().st_mtime == self._dir_mtime:
                    return
            except OSError:
                pass
        self._build_cache()

    def _build_cache(self) -> None:
        self._cache.clear()
        if not self.dir.is_dir():
            self._dir_mtime = None
            return
        for sub in sorted(self.dir.iterdir()):
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
            self._cache[sub.name] = (
                meta.get("name") or sub.name,
                meta.get("description", ""),
                text,
                mtime,
            )
        try:
            self._dir_mtime = self.dir.stat().st_mtime
        except OSError:
            self._dir_mtime = None

    def list(self) -> list[dict]:
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
