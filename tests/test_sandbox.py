"""Filesystem sandbox enforcement: traversal, absolute paths, symlinks."""

from __future__ import annotations

import pytest

from tools import build_registry


@pytest.fixture
def reg(cfg):
    return build_registry(cfg)


def test_read_write_roundtrip(reg, cfg):
    out, ok = reg.execute("write_file", {"path": "notes/hello.txt", "content": "hi there"})
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
    out, ok = reg.execute("write_file", {"path": "../escape.txt", "content": "x"})
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
    assert out.endswith("[truncated]")
    assert len(out) <= cfg.FS_READ_LIMIT + len("\n[truncated]")


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
