"""Filesystem sandbox enforcement: traversal, absolute paths, symlinks."""

from __future__ import annotations

import pytest

from tools import build_registry


@pytest.fixture
def reg(cfg):
    return build_registry(cfg)


def test_read_write_roundtrip(reg, cfg):
    (cfg.root / "notes").mkdir()
    out, ok = reg.execute("edit_file", {"path": "notes/hello.txt", "action": "create", "content": "hi there"})
    assert ok, out
    assert (cfg.root / "notes/hello.txt").read_text() == "hi there"
    out, ok = reg.execute("read_file", {"path": "notes/hello.txt"})
    assert ok
    assert out == "hi there"


def test_list_directory(reg, cfg):
    (cfg.root / "sub").mkdir()
    (cfg.root / "a.txt").write_text("x")
    (cfg.root / "sub" / "b.txt").write_text("y")
    out, ok = reg.execute("list_directory", {"path": "."})
    assert ok
    assert "a.txt" in out
    assert "sub/" in out  # directories marked


def test_relative_traversal_rejected(reg, cfg):
    (cfg.root.parent / "outside.txt").write_text("secret")
    out, ok = reg.execute("read_file", {"path": "../outside.txt"})
    assert ok is False
    assert "ERROR" in out
    out, ok = reg.execute("read_file", {"path": "a/../../outside.txt"})
    assert ok is False


def test_absolute_path_rejected(reg):
    out, ok = reg.execute("read_file", {"path": "/etc/passwd"})
    assert ok is False
    assert "ERROR" in out


def test_write_traversal_rejected(reg, cfg):
    out, ok = reg.execute("edit_file", {"path": "../escape.txt", "action": "create", "content": "x"})
    assert ok is False
    assert not (cfg.root.parent / "escape.txt").exists()


def test_symlink_escape_rejected(reg, cfg, tmp_path):
    outside_dir = tmp_path.parent / "outside"
    outside_dir.mkdir(exist_ok=True)
    outside = outside_dir / "outside.txt"
    outside.write_text("top secret")
    link = cfg.root / "link.txt"
    link.symlink_to(outside)
    out, ok = reg.execute("read_file", {"path": "link.txt"})
    assert ok is False
    assert "ERROR" in out


def test_symlinked_directory_escape_rejected(reg, cfg, tmp_path):
    outside_dir = tmp_path.parent / "outside_dir"
    outside_dir.mkdir(exist_ok=True)
    (outside_dir / "leak.txt").write_text("secret")
    (cfg.root / "linkdir").symlink_to(outside_dir)
    out, ok = reg.execute("read_file", {"path": "linkdir/leak.txt"})
    assert ok is False


def test_missing_file_is_error_not_crash(reg):
    out, ok = reg.execute("read_file", {"path": "does_not_exist.txt"})
    assert ok is False
    assert "not found" in out


def test_empty_path_rejected(reg):
    out, ok = reg.execute("read_file", {"path": "   "})
    assert ok is False


def test_read_truncation(reg, cfg):
    (cfg.root / "big.txt").write_text("x" * (cfg.FS_READ_LIMIT + 100))
    out, ok = reg.execute("read_file", {"path": "big.txt"})
    assert ok
    assert "truncated" in out
    assert len(out) < cfg.FS_READ_LIMIT + 200


# --- rename_file ----------------------------------------------------------


def test_rename_success(reg, cfg):
    (cfg.root / "a.txt").write_text("1")
    out, ok = reg.execute("rename_file", {"path": "a.txt", "new_name": "b.txt"})
    assert ok, out
    assert not (cfg.root / "a.txt").exists()
    assert (cfg.root / "b.txt").read_text() == "1"


def test_rename_in_subdir_stays_in_subdir(reg, cfg):
    (cfg.root / "s").mkdir()
    (cfg.root / "s" / "a.txt").write_text("1")
    out, ok = reg.execute("rename_file", {"path": "s/a.txt", "new_name": "b.txt"})
    assert ok, out
    assert (cfg.root / "s" / "b.txt").read_text() == "1"
    assert not (cfg.root / "b.txt").exists()


def test_rename_missing_file(reg):
    out, ok = reg.execute("rename_file", {"path": "nope.txt", "new_name": "b.txt"})
    assert ok is False
    assert "ERROR" in out


def test_rename_directory_rejected(reg, cfg):
    (cfg.root / "adir").mkdir()
    out, ok = reg.execute("rename_file", {"path": "adir", "new_name": "bdir"})
    assert ok is False
    assert "ERROR" in out


def test_rename_existing_dest_rejected(reg, cfg):
    (cfg.root / "a.txt").write_text("1")
    (cfg.root / "b.txt").write_text("2")
    out, ok = reg.execute("rename_file", {"path": "a.txt", "new_name": "b.txt"})
    assert ok is False
    assert "ERROR" in out
    assert (cfg.root / "a.txt").exists()


def test_rename_new_name_with_slash_rejected(reg, cfg):
    (cfg.root / "a.txt").write_text("1")
    out, ok = reg.execute("rename_file", {"path": "a.txt", "new_name": "x/y.txt"})
    assert ok is False
    assert "ERROR" in out
    assert (cfg.root / "a.txt").exists()


def test_rename_new_name_dotdot_rejected(reg, cfg):
    (cfg.root / "a.txt").write_text("1")
    for bad in (".", ".."):
        out, ok = reg.execute("rename_file", {"path": "a.txt", "new_name": bad})
        assert ok is False
        assert "ERROR" in out


def test_rename_new_name_empty_rejected(reg, cfg):
    (cfg.root / "a.txt").write_text("1")
    out, ok = reg.execute("rename_file", {"path": "a.txt", "new_name": ""})
    assert ok is False
    assert "ERROR" in out


# --- move_file ------------------------------------------------------------


def test_move_success_same_dir(reg, cfg):
    (cfg.root / "a.txt").write_text("data")
    out, ok = reg.execute("move_file", {"source": "a.txt", "destination": "b.txt"})
    assert ok, out
    assert not (cfg.root / "a.txt").exists()
    assert (cfg.root / "b.txt").read_text() == "data"


def test_move_creates_destination_parent(reg, cfg):
    (cfg.root / "a.txt").write_text("data")
    out, ok = reg.execute("move_file", {"source": "a.txt", "destination": "x/y/b.txt"})
    assert ok, out
    assert (cfg.root / "x" / "y" / "b.txt").read_text() == "data"
    assert not (cfg.root / "a.txt").exists()


def test_move_missing_source(reg):
    out, ok = reg.execute("move_file", {"source": "nope.txt", "destination": "b.txt"})
    assert ok is False
    assert "ERROR" in out


def test_move_source_directory_rejected(reg, cfg):
    (cfg.root / "adir").mkdir()
    out, ok = reg.execute("move_file", {"source": "adir", "destination": "bdir"})
    assert ok is False
    assert "ERROR" in out


def test_move_existing_destination_rejected(reg, cfg):
    (cfg.root / "a.txt").write_text("1")
    (cfg.root / "b.txt").write_text("2")
    out, ok = reg.execute("move_file", {"source": "a.txt", "destination": "b.txt"})
    assert ok is False
    assert "ERROR" in out
    assert (cfg.root / "a.txt").exists()


# --- edit_file: str_replace ------------------------------------------------


def test_edit_str_replace_unique(reg, cfg):
    (cfg.root / "a.txt").write_text("foo bar baz\nfoo qux\n")
    out, ok = reg.execute(
        "edit_file",
        {"path": "a.txt", "action": "str_replace", "old_str": "foo bar baz", "new_str": "hello world"},
    )
    assert ok, out
    assert (cfg.root / "a.txt").read_text() == "hello world\nfoo qux\n"


def test_edit_str_replace_duplicate(reg, cfg):
    (cfg.root / "a.txt").write_text("dup\ndup\n")
    out, ok = reg.execute(
        "edit_file",
        {"path": "a.txt", "action": "str_replace", "old_str": "dup", "new_str": "x"},
    )
    assert ok is False
    assert "not unique" in out
    assert (cfg.root / "a.txt").read_text() == "dup\ndup\n"


def test_edit_str_replace_no_match(reg, cfg):
    (cfg.root / "a.txt").write_text("hello\n")
    out, ok = reg.execute(
        "edit_file",
        {"path": "a.txt", "action": "str_replace", "old_str": "zebra", "new_str": "x"},
    )
    assert ok is False
    assert "not found" in out
    assert (cfg.root / "a.txt").read_text() == "hello\n"


def test_edit_str_replace_missing_file(reg):
    out, ok = reg.execute(
        "edit_file",
        {"path": "nope.txt", "action": "str_replace", "old_str": "a", "new_str": "b"},
    )
    assert ok is False
    assert "not found" in out


# --- edit_file: create -----------------------------------------------------


def test_edit_create_new_file(reg, cfg):
    out, ok = reg.execute(
        "edit_file",
        {"path": "new.txt", "action": "create", "content": "fresh content"},
    )
    assert ok, out
    assert (cfg.root / "new.txt").read_text() == "fresh content"


def test_edit_create_overwrite(reg, cfg):
    (cfg.root / "existing.txt").write_text("old")
    out, ok = reg.execute(
        "edit_file",
        {"path": "existing.txt", "action": "create", "content": "new"},
    )
    assert ok, out
    assert (cfg.root / "existing.txt").read_text() == "new"


def test_edit_create_missing_subdir(reg):
    out, ok = reg.execute(
        "edit_file",
        {"path": "no/such/dir/file.txt", "action": "create", "content": "x"},
    )
    assert ok is False
    assert "ERROR" in out


# --- edit_file: insert -----------------------------------------------------


def test_edit_insert_prepend(reg, cfg):
    (cfg.root / "a.txt").write_text("line1\nline2\n")
    out, ok = reg.execute(
        "edit_file",
        {"path": "a.txt", "action": "insert", "line": 1, "text": "first"},
    )
    assert ok, out
    assert (cfg.root / "a.txt").read_text() == "first\nline1\nline2\n"


def test_edit_insert_append(reg, cfg):
    (cfg.root / "a.txt").write_text("line1\nline2\n")
    out, ok = reg.execute(
        "edit_file",
        {"path": "a.txt", "action": "insert", "line": 100, "text": "last"},
    )
    assert ok, out
    assert (cfg.root / "a.txt").read_text() == "line1\nline2\nlast\n"


def test_edit_insert_mid_file(reg, cfg):
    (cfg.root / "a.txt").write_text("line1\nline2\nline3\n")
    out, ok = reg.execute(
        "edit_file",
        {"path": "a.txt", "action": "insert", "line": 2, "text": "inserted"},
    )
    assert ok, out
    assert (cfg.root / "a.txt").read_text() == "line1\ninserted\nline2\nline3\n"


def test_edit_insert_clamp_zero(reg, cfg):
    (cfg.root / "a.txt").write_text("line1\n")
    out, ok = reg.execute(
        "edit_file",
        {"path": "a.txt", "action": "insert", "line": 0, "text": "top"},
    )
    assert ok, out
    assert (cfg.root / "a.txt").read_text() == "top\nline1\n"


def test_edit_insert_missing_file(reg):
    out, ok = reg.execute(
        "edit_file",
        {"path": "nope.txt", "action": "insert", "line": 1, "text": "x"},
    )
    assert ok is False
    assert "not found" in out


# --- read_file: line range -------------------------------------------------


def test_read_range_basic(reg, cfg):
    (cfg.root / "a.txt").write_text("l1\nl2\nl3\nl4\nl5\n")
    out, ok = reg.execute("read_file", {"path": "a.txt", "start_line": 2, "end_line": 4})
    assert ok, out
    assert "   2  l2" in out
    assert "   3  l3" in out
    assert "   4  l4" in out
    assert "l1" not in out
    assert "l5" not in out


def test_read_range_past_eof(reg, cfg):
    (cfg.root / "a.txt").write_text("l1\nl2\nl3\n")
    out, ok = reg.execute("read_file", {"path": "a.txt", "start_line": 2, "end_line": 100})
    assert ok, out
    assert "   2  l2" in out
    assert "   3  l3" in out


def test_read_range_empty_file(reg, cfg):
    (cfg.root / "a.txt").write_text("")
    out, ok = reg.execute("read_file", {"path": "a.txt", "start_line": 1, "end_line": 5})
    assert ok, out
    assert out == ""


def test_read_range_single_line(reg, cfg):
    (cfg.root / "a.txt").write_text("a\nb\nc\n")
    out, ok = reg.execute("read_file", {"path": "a.txt", "start_line": 2, "end_line": 2})
    assert ok, out
    assert "   2  b" in out
    assert "a" not in out.split("  ")[1] if "  " in out else True


def test_read_range_start_past_eof(reg, cfg):
    (cfg.root / "a.txt").write_text("l1\n")
    out, ok = reg.execute("read_file", {"path": "a.txt", "start_line": 10, "end_line": 20})
    assert ok, out
    assert out == ""


def test_read_no_range_full_content(reg, cfg):
    (cfg.root / "a.txt").write_text("hello\nworld\n")
    out, ok = reg.execute("read_file", {"path": "a.txt"})
    assert ok, out
    assert out == "hello\nworld\n"
