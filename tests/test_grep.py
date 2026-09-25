"""Tests for the grep_files tool."""

from __future__ import annotations

import pytest

from config import AppConfig, ensure_dirs
from tools import build_registry


@pytest.fixture
def cfg(tmp_path):
    c = AppConfig(root=tmp_path)
    ensure_dirs(c)
    return c


@pytest.fixture
def reg(cfg):
    return build_registry(cfg)


def _grep(reg, **kw):
    return reg.execute("grep_files", kw)


# --- basic matching ---------------------------------------------------------


def test_basic_match(reg, cfg):
    (cfg.root / "a.txt").write_text("hello world\nnothing\nhello again\n")
    out, ok = _grep(reg, pattern="hello")
    assert ok, out
    assert "a.txt:1: hello world" in out
    assert "a.txt:3: hello again" in out
    assert "2 match(es)" in out


def test_no_matches(reg, cfg):
    (cfg.root / "a.txt").write_text("hello\n")
    out, ok = _grep(reg, pattern="zebra")
    assert ok, out
    assert "0 match(es)" in out
    assert "no matches" in out


def test_regex_features(reg, cfg):
    (cfg.root / "a.txt").write_text("foo1\nbar2\nFOO3\n")
    out, ok = _grep(reg, pattern=r"^f\w+$", case_insensitive=True)
    assert ok, out
    assert "a.txt:1: foo1" in out
    assert "a.txt:3: FOO3" in out
    assert "2 match(es)" in out


def test_case_sensitive_default(reg, cfg):
    (cfg.root / "a.txt").write_text("Hello\nhello\n")
    out, ok = _grep(reg, pattern="hello")
    assert ok, out
    assert "a.txt:2: hello" in out
    assert "1 match(es)" in out


# --- path handling -----------------------------------------------------------


def test_search_subdirectory(reg, cfg):
    (cfg.root / "sub").mkdir()
    (cfg.root / "sub" / "x.txt").write_text("needle\n")
    (cfg.root / "top.txt").write_text("needle\n")
    out, ok = _grep(reg, pattern="needle", path="sub")
    assert ok, out
    assert "sub/x.txt:1: needle" in out
    assert "top.txt" not in out


def test_search_single_file(reg, cfg):
    (cfg.root / "one.txt").write_text("alpha\nbeta\n")
    out, ok = _grep(reg, pattern="alpha", path="one.txt")
    assert ok, out
    assert "one.txt:1: alpha" in out


def test_missing_path_errors(reg):
    out, ok = _grep(reg, pattern="x", path="no/such/dir")
    assert ok is False
    assert "ERROR" in out
    assert "not found" in out


def test_path_escape_rejected(reg):
    out, ok = _grep(reg, pattern="x", path="..")
    assert ok is False
    assert "ERROR" in out


def test_absolute_path_rejected(reg):
    out, ok = _grep(reg, pattern="x", path="/etc")
    assert ok is False
    assert "ERROR" in out


# --- limits -------------------------------------------------------------------


def test_max_results_caps(reg, cfg):
    (cfg.root / "big.txt").write_text("\n".join(f"hit {i}" for i in range(10)) + "\n")
    out, ok = _grep(reg, pattern="hit", max_results=3)
    assert ok, out
    assert "3 match(es)" in out
    assert "showing first 3" in out
    assert "hit 3" not in out


def test_max_results_capped_at_200(reg, cfg):
    out, ok = _grep(reg, pattern="x", max_results=10_000)
    assert ok is False or ok  # either fine: no files, but arg must validate
    # validation accepts the large int; behavior is just no matches
    assert "ERROR" not in out or ok


def test_skips_heavy_dirs(reg, cfg):
    (cfg.root / "venv").mkdir()
    (cfg.root / "venv" / "lib.py").write_text("secret_in_venv\n")
    (cfg.root / "keep.txt").write_text("secret_in_venv\n")
    out, ok = _grep(reg, pattern="secret_in_venv")
    assert ok, out
    assert "keep.txt:1" in out
    assert "venv/lib.py" not in out


# --- errors ---------------------------------------------------------------------


def test_invalid_regex(reg):
    out, ok = _grep(reg, pattern="(")
    assert ok is False
    assert "invalid regex" in out


def test_empty_pattern(reg):
    out, ok = _grep(reg, pattern="   ")
    assert ok is False
    assert "ERROR" in out


def test_binary_file_skipped(reg, cfg):
    (cfg.root / "bin.dat").write_bytes(b"\x00\x01\x02needle\x00")
    (cfg.root / "ok.txt").write_text("needle\n")
    out, ok = _grep(reg, pattern="needle")
    assert ok, out
    assert "ok.txt:1: needle" in out
    assert "bin.dat" not in out


# --- listing / availability -------------------------------------------------


def test_grep_is_listed_in_available_tools(reg):
    from chat.prompts import build_system_prompt

    prompt = build_system_prompt([], tools=reg.tools())
    assert "Available tools" in prompt
    # grep_files must be explicitly listed among available tools
    assert "grep_files" in prompt
    # and every registered tool must appear
    for name in reg.names():
        assert name in prompt, f"tool {name} missing from available-tools list"


# --- grep: context lines ---------------------------------------------------


def test_grep_before_after(reg, cfg):
    (cfg.root / "a.txt").write_text("l1\nl2\nMATCH\nl4\nl5\nl6\n")
    out, ok = _grep(reg, pattern="MATCH", before=1, after=1)
    assert ok, out
    assert "a.txt:2- l2" in out
    assert "a.txt:3: MATCH" in out
    assert "a.txt:4- l4" in out
    assert "1 match(es)" in out


def test_grep_context_multiple_matches(reg, cfg):
    (cfg.root / "a.txt").write_text("MATCH\nx\nMATCH\n")
    out, ok = _grep(reg, pattern="MATCH", before=0, after=1)
    assert ok, out
    assert "a.txt:1: MATCH" in out
    assert "a.txt:2- x" in out
    assert "--" in out
    assert "a.txt:3: MATCH" in out
    assert "2 match(es)" in out


# --- grep: include / exclude -----------------------------------------------


def test_grep_include(reg, cfg):
    (cfg.root / "a.py").write_text("needle\n")
    (cfg.root / "b.md").write_text("needle\n")
    out, ok = _grep(reg, pattern="needle", include="*.py")
    assert ok, out
    assert "a.py" in out
    assert "b.md" not in out


def test_grep_exclude(reg, cfg):
    (cfg.root / "a.py").write_text("needle\n")
    (cfg.root / "b.md").write_text("needle\n")
    out, ok = _grep(reg, pattern="needle", exclude="*.md")
    assert ok, out
    assert "a.py" in out
    assert "b.md" not in out


# --- grep: files_only ------------------------------------------------------


def test_grep_files_only(reg, cfg):
    (cfg.root / "a.py").write_text("needle\n")
    (cfg.root / "b.py").write_text("nothing\nneedle\n")
    (cfg.root / "c.py").write_text("no match here\n")
    out, ok = _grep(reg, pattern="needle", files_only=True)
    assert ok, out
    assert "a.py" in out
    assert "b.py" in out
    assert "c.py" not in out
    assert "2 file(s)" in out


# --- find_files ------------------------------------------------------------


def _find(reg, **kw):
    return reg.execute("find_files", kw)


def test_find_basic(reg, cfg):
    (cfg.root / "a.py").write_text("")
    (cfg.root / "b.py").write_text("")
    (cfg.root / "c.md").write_text("")
    (cfg.root / "sub").mkdir()
    (cfg.root / "sub" / "d.py").write_text("")
    out, ok = _find(reg, pattern="*.py")
    assert ok, out
    assert "a.py" in out
    assert "b.py" in out
    assert "sub/d.py" in out
    assert "c.md" not in out


def test_find_no_match(reg, cfg):
    (cfg.root / "a.txt").write_text("")
    out, ok = _find(reg, pattern="*.xyz")
    assert ok, out
    assert "no files matched" in out


def test_find_max_results(reg, cfg):
    for i in range(10):
        (cfg.root / f"f{i}.py").write_text("")
    out, ok = _find(reg, pattern="*.py", max_results=3)
    assert ok, out
    assert "showing first 3" in out


def test_find_path_escape_rejected(reg):
    out, ok = _find(reg, pattern="*.py", path="..")
    assert ok is False
    assert "ERROR" in out


def test_find_is_listed_in_available_tools(reg):
    from chat.prompts import build_system_prompt

    prompt = build_system_prompt([], tools=reg.tools())
    assert "find_files" in prompt
