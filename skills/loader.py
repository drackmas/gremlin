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

    def list(self) -> list[dict]:
        out: list[dict] = []
        if not self.dir.is_dir():
            return out
        for sub in sorted(self.dir.iterdir()):
            skill_file = sub / "SKILL.md"
            if not sub.is_dir() or not skill_file.is_file():
                continue
            try:
                meta, _ = parse_frontmatter(skill_file.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError) as e:
                log.warning("skipping unreadable skill %s: %s", sub.name, e)
                continue
            out.append(
                {
                    "slug": sub.name,
                    "name": meta.get("name") or sub.name,
                    "description": meta.get("description", ""),
                }
            )
        return out

    def get(self, name: str) -> str:
        """Full skill text by name or slug (case-insensitive)."""
        want = (name or "").strip().lower()
        for sub in sorted(self.dir.iterdir()) if self.dir.is_dir() else []:
            skill_file = sub / "SKILL.md"
            if not sub.is_dir() or not skill_file.is_file():
                continue
            try:
                meta, body = parse_frontmatter(skill_file.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError):
                continue
            if want in (sub.name.lower(), meta.get("name", "").lower()):
                return skill_file.read_text(encoding="utf-8")
        raise SkillNotFoundError(name)
