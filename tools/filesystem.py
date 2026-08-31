"""Sandboxed filesystem tools.

The sandbox root is the project root. All paths are model-supplied and are
treated as untrusted: relative-only, normalized, resolved through symlinks,
and verified to stay inside the root. Enforcement lives here in code, not in
the prompt.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from .registry import Tool


class SandboxError(Exception):
    """Raised when a path would escape the sandbox."""


def _resolve(root: Path, path: str) -> Path:
    """Resolve ``path`` inside ``root``. Raises SandboxError on escape."""
    if not isinstance(path, str) or not path.strip():
        raise SandboxError("path must be a non-empty string")
    if os.path.isabs(path):
        raise SandboxError(f"absolute paths are not allowed: {path}")
    norm = os.path.normpath(path)
    if norm == ".." or norm.startswith(".." + os.sep) or (os.sep != "/" and norm.startswith("..\\")):
        raise SandboxError(f"path escapes sandbox: {path}")
    root_real = os.path.realpath(root)
    real = os.path.realpath(os.path.join(root_real, norm))
    if os.path.commonpath([root_real, real]) != root_real:
        raise SandboxError(f"path escapes sandbox: {path}")
    return Path(real)


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


def build_fs_tools(cfg, read_limit: int) -> list[Tool]:
    root = Path(cfg.root)

    def read_file(args: dict) -> str:
        p = _resolve(root, args["path"])
        if not p.exists():
            raise SandboxError(f"file not found: {args['path']}")
        if not p.is_file():
            raise SandboxError(f"not a file: {args['path']}")
        try:
            raw = p.read_bytes()[: read_limit + 1]
        except OSError as e:
            raise SandboxError(f"cannot read {args['path']}: {e}") from e
        text = raw.decode("utf-8", errors="replace")
        if len(raw) > read_limit:
            text = text[:read_limit] + "\n[truncated]"
        return text

    def write_file(args: dict) -> str:
        p = _resolve(root, args["path"])
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(args["content"], encoding="utf-8")
        except OSError as e:
            raise SandboxError(f"cannot write {args['path']}: {e}") from e
        return f"wrote {len(args['content'])} bytes to {args['path']}"

    def list_directory(args: dict) -> str:
        p = _resolve(root, args.get("path") or ".")
        if not p.exists() or not p.is_dir():
            raise SandboxError(f"not a directory: {args.get('path') or '.'}")
        try:
            entries = sorted(p.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower()))
        except OSError as e:
            raise SandboxError(f"cannot list {args.get('path')}: {e}") from e
        if not entries:
            return "(empty directory)"
        return "\n".join(e.name + ("/" if e.is_dir() else "") for e in entries)

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

    return [
        Tool(
            name="read_file",
            description="Read a text file from the project workspace (relative path from project root).",
            parameters={
                "type": "object",
                "properties": {"path": {"type": "string", "description": "relative file path"}},
                "required": ["path"],
            },
            handler=read_file,
        ),
        Tool(
            name="write_file",
            description="Create or overwrite a text file in the project workspace (relative path). Parent directories are created.",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "relative file path"},
                    "content": {"type": "string", "description": "full file content"},
                },
                "required": ["path", "content"],
            },
            handler=write_file,
        ),
        Tool(
            name="list_directory",
            description="List files and subdirectories of a directory in the project workspace (relative path; '.' for root). Directories end with '/'.",
            parameters={
                "type": "object",
                "properties": {"path": {"type": "string", "description": "relative directory path, default '.'"}},
                "required": [],
            },
            handler=list_directory,
        ),
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
    ]
