"""Sandboxed filesystem tools.

The sandbox root is the project root. All paths are model-supplied and are
treated as untrusted: relative-only, normalized, resolved through symlinks,
and verified to stay inside the root. Enforcement lives here in code, not in
the prompt.
"""

from __future__ import annotations

import os
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
    ]
