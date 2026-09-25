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
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            raise SandboxError(f"cannot read {args['path']}: {e}") from e
        if len(text) > read_limit and not args.get("start_line") and not args.get("end_line"):
            return text[:read_limit] + f"\n[truncated: file is {len(text)} bytes, showing first {read_limit}]"
        start_line = args.get("start_line")
        end_line = args.get("end_line")
        if start_line is not None or end_line is not None:
            lines = text.splitlines(keepends=True)
            total = len(lines)
            if total == 0:
                return ""
            s = max(1, int(start_line or 1))
            end = min(total, int(end_line or total))
            if s > total:
                return ""
            numbered = [f"{i:4d}  {ln.rstrip()}" for i, ln in enumerate(lines[s - 1 : end], s)]
            return "\n".join(numbered)
        return text

    def edit_file(args: dict) -> str:
        action = args["action"]
        path = args["path"]
        p = _resolve(root, path)

        if action == "str_replace":
            old_str = args["old_str"]
            new_str = args["new_str"]
            if not p.exists():
                raise SandboxError(f"file not found: {path}")
            if not p.is_file():
                raise SandboxError(f"not a file: {path}")
            try:
                text = p.read_text(encoding="utf-8")
            except OSError as e:
                raise SandboxError(f"cannot read {path}: {e}") from e
            count = text.count(old_str)
            if count == 0:
                raise SandboxError(f"old_str not found in {path}")
            if count > 1:
                raise SandboxError(f"old_str is not unique in {path} ({count} occurrences)")
            new_text = text.replace(old_str, new_str, 1)
            try:
                p.write_text(new_text, encoding="utf-8")
            except OSError as e:
                raise SandboxError(f"cannot write {path}: {e}") from e
            return f"replaced old_str in {path}"

        elif action == "create":
            content = args["content"]
            if not p.parent.is_dir():
                raise SandboxError(f"parent directory does not exist: {p.parent}")
            try:
                p.write_text(content, encoding="utf-8")
            except OSError as e:
                raise SandboxError(f"cannot write {path}: {e}") from e
            return f"wrote {len(content)} bytes to {path}"

        elif action == "insert":
            line = args["line"]
            text = args["text"]
            if not p.exists():
                raise SandboxError(f"file not found: {path}")
            if not p.is_file():
                raise SandboxError(f"not a file: {path}")
            try:
                content = p.read_text(encoding="utf-8")
            except OSError as e:
                raise SandboxError(f"cannot read {path}: {e}") from e
            lines = content.splitlines(keepends=True)
            total = len(lines)
            ln = max(1, min(int(line), total + 1))
            if not text.endswith("\n"):
                text = text + "\n"
            lines.insert(ln - 1, text)
            new_content = "".join(lines)
            try:
                p.write_text(new_content, encoding="utf-8")
            except OSError as e:
                raise SandboxError(f"cannot write {path}: {e}") from e
            return f"inserted {len(text)} bytes at line {ln} in {path}"

        else:
            raise SandboxError(f"unknown action: {action}")

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
            description="Read a text file from the project workspace (relative path from project root). "
            "Optionally read a line range with start_line and end_line (1-based, inclusive); "
            "range output includes line numbers.",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "relative file path"},
                    "start_line": {"type": "integer", "description": "first line to read (1-based, inclusive)"},
                    "end_line": {"type": "integer", "description": "last line to read (1-based, inclusive)"},
                },
                "required": ["path"],
            },
            handler=read_file,
        ),
        Tool(
            name="edit_file",
            description="Edit a file in the project workspace. Actions: "
            "str_replace (replace a unique old_str with new_str), "
            "create (write full content to a new or existing file; parent dir must exist), "
            "insert (insert text at a 1-based line number; 0 clamps to 1, past EOF appends).",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "relative file path"},
                    "action": {
                        "type": "string",
                        "enum": ["str_replace", "create", "insert"],
                        "description": "edit action to perform",
                    },
                    "old_str": {"type": "string", "description": "exact text to find (str_replace; must be unique in file)"},
                    "new_str": {"type": "string", "description": "replacement text (str_replace)"},
                    "content": {"type": "string", "description": "full file content (create)"},
                    "line": {"type": "integer", "description": "1-based line number to insert at (insert)"},
                    "text": {"type": "string", "description": "text to insert (insert)"},
                },
                "required": ["path", "action"],
            },
            handler=edit_file,
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
