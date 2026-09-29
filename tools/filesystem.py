"""Filesystem tools: sandboxed read, edit, list, rename, and move operations."""

from __future__ import annotations

from pathlib import Path

from .registry import Tool
from .sandbox import SandboxError, _bare_name, _resolve


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
                raise SandboxError(f"old_str is not unique in {path} (found {count} occurrences)")
            p.write_text(text.replace(old_str, new_str, 1), encoding="utf-8")
            return f"replaced {len(old_str)} chars with {len(new_str)} chars in {path}"

        elif action == "create":
            content = args["content"]
            if p.is_dir():
                raise SandboxError(f"not a file: {path}")
            try:
                p.write_text(content, encoding="utf-8")
            except OSError as e:
                raise SandboxError(f"cannot create {path}: {e}") from e
            return f"created {path} ({len(content)} bytes)"

        elif action == "insert":
            line = args.get("line", 1)
            text = args["text"]
            if not p.exists():
                raise SandboxError(f"file not found: {path}")
            if p.is_dir():
                raise SandboxError(f"not a file: {path}")
            try:
                existing = p.read_text(encoding="utf-8")
            except OSError as e:
                raise SandboxError(f"cannot read {path}: {e}") from e
            lines = existing.splitlines(keepends=True)
            idx = max(0, min(int(line) - 1, len(lines)))
            lines.insert(idx, text + "\n")
            try:
                p.write_text("".join(lines), encoding="utf-8")
            except OSError as e:
                raise SandboxError(f"cannot insert into {path}: {e}") from e
            return f"inserted at line {idx + 1} in {path}"

        raise SandboxError(f"unknown edit action: {action}")

    def list_directory(args: dict) -> str:
        path = args.get("path") or "."
        p = _resolve(root, path)
        if not p.is_dir():
            raise SandboxError(f"not a directory: {path}")
        try:
            entries = sorted(p.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower()))
        except OSError as e:
            raise SandboxError(f"cannot list {path}: {e}") from e
        if not entries:
            return "(empty directory)"
        return "\n".join(e.name + ("/" if e.is_dir() else "") for e in entries)

    def rename_file(args: dict) -> str:
        src = _resolve(root, args["path"])
        if not src.is_file():
            raise SandboxError(f"file not found: {args['path']}")
        new_name = _bare_name(args["new_name"])
        dst = src.with_name(new_name)
        if dst.exists():
            raise SandboxError(f"destination already exists: {new_name}")
        try:
            src.rename(dst)
        except OSError as e:
            raise SandboxError(f"cannot rename: {e}") from e
        return f"renamed to '{new_name}' (same directory as {args['path']})"

    def move_file(args: dict) -> str:
        src = _resolve(root, args["source"])
        if not src.is_file():
            raise SandboxError(f"file not found: {args['source']}")
        dst = _resolve(root, args["destination"])
        if dst.exists():
            raise SandboxError(f"destination already exists: {args['destination']}")
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            src.rename(dst)
        except OSError as e:
            raise SandboxError(f"cannot move: {e}") from e
        return f"moved {args['source']} -> {args['destination']}"

    return [
        Tool(
            name="read_file",
            description="Read a file from the project workspace. Returns the file content "
                        "(truncated to the read limit). Optionally read a line range "
                        "(start_line, end_line, 1-based, inclusive).",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "relative file path"},
                    "start_line": {"type": "integer", "description": "1-based start line (optional)"},
                    "end_line": {"type": "integer", "description": "1-based end line, inclusive (optional)"},
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
