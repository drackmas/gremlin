# Plan: new sandbox tools `rename_file` and `move_file`

## Context / goal
Add two file operations to the sandboxed filesystem tool set (alongside
`read_file`, `write_file`, `list_directory`):
- `rename_file(path, new_name)` — rename a file **within its own directory**.
- `move_file(source, destination)` — move a file to a new path (possibly a
  different subdirectory of the sandbox).

Both must be sandbox-safe (no escape), non-overwriting, files-only (no
directories), and exposed to the model through the existing `ToolRegistry`.

## Key facts from exploration
- `tools/filesystem.py` (`#B778`, 110 lines): `_resolve(root, path)` (lines
  21-34) already rejects absolute paths, `..` after normalization, and any
  `realpath`/`commonpath` escape, raising `SandboxError`. `build_fs_tools(cfg,
  fs_read_limit)` builds closures and returns a `list[Tool]` (lines 37-110).
  `_resolve` returns the **realpath**, so `src.parent` is a real path under the
  resolved root.
- `tools/registry.py`: `Tool` dataclass (lines 34-42), `execute` (lines
  146-181) validates args via `_validate_args` (lines 84-126) using
  `type_map` (line 87) and `required`, catches handler exceptions →
  `("ERROR: <msg>", False)`. `to_openai_tools()` (lines 128-132) is generic.
- Tools reach the model via `self.registry.to_openai_tools()` at
  chat/manager.py:91 — no hardcoded tool list; registering in `build_fs_tools`
  is sufficient.
- `build_registry(cfg, loader=None)` (tools/__init__.py:22) calls
  `build_fs_tools(cfg, cfg.FS_READ_LIMIT)`; no change needed there.
- Test fixture `cfg` = `AppConfig(root=tmp_path)` + `ensure_dirs`
  (tests/conftest.py); `reg = build_registry(cfg)` fixture in
  tests/test_sandbox.py:10. Existing sandbox tests already write to
  `cfg.root.parent / "outside.txt"` — reuse that pattern for escape tests.
- `SandboxError` (tools/filesystem.py:17) is the project's escape/error type.

## Approach

### 1. `tools/filesystem.py` — add imports
After `import os` (line 11), add `import shutil` (used by `move_file`).

### 2. `tools/filesystem.py` — add a bare-filename validator
Add at module level (near `_resolve`, e.g. right after it):
```python
def _bare_name(name: str) -> str:
    """Validate that ``name`` is a bare filename (no path). Returns it stripped.

    Raises SandboxError for empty, ``.``/``..``, or any path separator.
    """
    if not isinstance(name, str):
        raise SandboxError("new_name must be a string")
    name = name.strip()
    if not name:
        raise SandboxError("new_name must be a non-empty filename")
    if name in (".", ".."):
        raise SandboxError(f"invalid new_name: {name}")
    if "/" in name or "\\" in name:
        raise SandboxError("new_name must be a filename, not a path")
    return name
```
Rationale: checking both `/` and `\\` covers posix and Windows separators.

### 3. `tools/filesystem.py` — add the two handlers inside `build_fs_tools`
Add these two closures right after the `list_directory` closure (before the
`return [`):
```python
    def rename_file(args: dict) -> str:
        src = _resolve(root, args["path"])
        if not src.exists():
            raise SandboxError(f"file not found: {args['path']}")
        if not src.is_file():
            raise SandboxError(f"not a file: {args['path']}")
        new_name = _bare_name(args["new_name"])
        dest = src.parent / new_name
        if dest.exists():
            raise SandboxError(f"destination already exists: {new_name}")
        try:
            src.rename(dest)
        except OSError as e:
            raise SandboxError(f"cannot rename {args['path']}: {e}") from e
        return f"renamed to '{new_name}' (same directory as {args['path']})"

    def move_file(args: dict) -> str:
        src = _resolve(root, args["source"])
        dst = _resolve(root, args["destination"])
        if not src.exists():
            raise SandboxError(f"source not found: {args['source']}")
        if not src.is_file():
            raise SandboxError(f"source is not a file: {args['source']}")
        if dst.exists():
            raise SandboxError(f"destination already exists: {args['destination']}")
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))
        except OSError as e:
            raise SandboxError(f"cannot move {args['source']}: {e}") from e
        return f"moved {args['source']} -> {args['destination']}"
```

### 4. `tools/filesystem.py` — register both in the returned list
Append to the `return [` list (after the `list_directory` Tool, ends line
109):
```python
        Tool(
            name="rename_file",
            description="Rename a file within its own directory (same folder). "
                        "The file must exist and must not be a directory. "
                        "Fails if a file with the new name already exists.",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Current path of the file (relative to sandbox root)."},
                    "new_name": {"type": "string", "description": "New bare filename (no directories, no slashes, not '.' or '..')."},
                },
                "required": ["path", "new_name"],
            },
            handler=rename_file,
        ),
        Tool(
            name="move_file",
            description="Move a file to a new path inside the sandbox, "
                        "optionally into a different (possibly new) subdirectory. "
                        "The source must be an existing file, not a directory. "
                        "Fails if the destination already exists.",
            parameters={
                "type": "object",
                "properties": {
                    "source": {"type": "string", "description": "Current file path (relative to sandbox root)."},
                    "destination": {"type": "string", "description": "New file path (relative to sandbox root); parent dirs are created if missing."},
                },
                "required": ["source", "destination"],
            },
            handler=move_file,
        ),
```

### 5. `tests/test_sandbox.py` — add tests
Append (reuse `reg` + `cfg` fixtures):

```python
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
```

## Critical files & anchors
- `tools/filesystem.py` — `_resolve` (21-34) is the safety core; insert
  `_bare_name` after it, the two closures after `list_directory`, and the two
  `Tool(...)` entries into the `return [` list (ends line 110).
- `tests/test_sandbox.py` — `reg` fixture (line 10), existing escape-test
  pattern writing to `cfg.root.parent / "outside.txt"`.
- No changes to `tools/registry.py`, `tools/__init__.py`, `config.py`, or
  `chat/manager.py` — registration in `build_fs_tools` flows through
  `to_openai_tools()` automatically.

## Verification
1. `./venv/bin/python -m pytest tests/test_sandbox.py -v` — new + existing
   sandbox tests pass.
2. `./venv/bin/python -m pytest -q` — full suite green (no regressions).
3. Tool exposure check (logic only, no server):
   `./venv/bin/python -c "import tempfile; from config import AppConfig, ensure_dirs; from tools import build_registry; c=AppConfig(root=tempfile.mkdtemp()); ensure_dirs(c); print([t['function']['name'] for t in build_registry(c).to_openai_tools()])"`
   → includes `rename_file` and `move_file`.

## Assumptions & contingencies
- "Reject missing sources and directories" is interpreted as: reject a missing
  source file, and reject a directory passed where a file is expected (matches
  the requested "directories passed as files" test). The destination's parent
  may be created ("create it if appropriate"); the destination itself must not
  exist (no overwrite).
- `move_file` uses `shutil.move` (same- and cross-directory within the single
  sandbox filesystem); `rename_file` uses `Path.rename` (same directory only).
  If a parent path component of the destination is an existing *file*,
  `mkdir` raises `NotADirectoryError` (an `OSError`) converted to
  `SandboxError` — a deterministic failure.
- Return-message text is informational only; tests assert on filesystem state
  and `ok`/`ERROR`, not exact message wording, so wording can be tweaked
  freely.
- Sandbox-escape tests for `../` sources/destinations are covered by the
  existing `_resolve` guarantees already tested for read/write; the new
  handlers route every path through `_resolve`, so no new escape surface is
  introduced.
