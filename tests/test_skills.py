"""Skill discovery, loading, and the load_skill tool."""

from __future__ import annotations

import pytest

from skills.loader import SkillLoader, SkillNotFoundError, parse_frontmatter
from tools import build_registry

SKILL = """---
name: Alpha Skill
description: Does alpha things.
---

# Alpha

Instructions here.
"""


@pytest.fixture
def skills_dir(cfg):
    d = cfg.skills_dir / "alpha"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(SKILL)
    return cfg.skills_dir


def test_parse_frontmatter():
    meta, body = parse_frontmatter(SKILL)
    assert meta["name"] == "Alpha Skill"
    assert meta["description"] == "Does alpha things."
    assert body.startswith("# Alpha")


def test_parse_frontmatter_absent():
    meta, body = parse_frontmatter("just a body")
    assert meta == {}
    assert body == "just a body"


def test_discovery(skills_dir):
    skills = SkillLoader(type("C", (), {"skills_dir": skills_dir})()).list()
    assert len(skills) == 1
    assert skills[0]["name"] == "Alpha Skill"
    assert skills[0]["slug"] == "alpha"
    assert skills[0]["description"] == "Does alpha things."


def test_get_by_name_and_slug(skills_dir):
    loader = SkillLoader(type("C", (), {"skills_dir": skills_dir})())
    assert "Instructions here" in loader.get("Alpha Skill")
    assert "Instructions here" in loader.get("ALPHA SKILL")
    assert "Instructions here" in loader.get("alpha")


def test_get_unknown_raises(skills_dir):
    loader = SkillLoader(type("C", (), {"skills_dir": skills_dir})())
    with pytest.raises(SkillNotFoundError):
        loader.get("nope")


def test_corrupt_skill_skipped(cfg):
    bad = cfg.skills_dir / "bad"
    bad.mkdir(parents=True)
    (bad / "SKILL.md").write_bytes(b"\xff\xfe\x00garbage")
    good = cfg.skills_dir / "good"
    good.mkdir()
    (good / "SKILL.md").write_text("---\nname: Good\ndescription: fine\n---\nbody")
    skills = SkillLoader(cfg).list()
    assert [s["slug"] for s in skills] == ["good"]


def test_non_skill_files_ignored(cfg):
    cfg.skills_dir.mkdir(parents=True, exist_ok=True)
    (cfg.skills_dir / "loose.md").write_text("not a skill")
    (cfg.skills_dir / "empty_dir").mkdir()
    assert SkillLoader(cfg).list() == []


def test_load_skill_tool(cfg, skills_dir):
    reg = build_registry(cfg)
    out, ok = reg.execute("load_skill", {"name": "alpha"})
    assert ok
    assert "Instructions here" in out
    out, ok = reg.execute("load_skill", {"name": "missing"})
    assert not ok
    assert "unknown skill" in out
    assert "Alpha Skill" in out  # lists available skills


def test_example_skills_present_in_project_root():
    """The shipped example skills must exist at the real project root."""
    from config import _default_root

    root = _default_root()
    loader = SkillLoader(type("C", (), {"skills_dir": root / "skills"})())
    names = {s["slug"] for s in loader.list()}
    assert {"project_explorer", "youtube_summary"} <= names
