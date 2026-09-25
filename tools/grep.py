"""Grep tool: regex line search over the sandboxed project tree.

Walks files under the project root (or a given relative subpath), skips heavy
directories (venv, .git, node_modules, ...) and binary/oversized files, and
returns ``file:line: content`` matches. Pure Python stdlib; no external deps.
"""

from __future__ import annotations

import fnmatch
import os
import re
from pathlib import Path

from .filesystem import _resolve
from .registry import Tool

from config import AppConfig


class GrepError(Exception):
    """Raised when grep cannot run: bad pattern, bad path, no root."""


SKIP_DIRS = {
    ".git",
    "__pycache__",
    "node_modules",
    "venv",
    ".venv",
    ".pytest_cache",
}
MAX_FILE_BYTES = 128 * 1024  # skip files larger than this
MAX_LINE_CHARS = 300  # truncate very long matching lines
MAX_RESULTS_CAP = 200  # hard cap on 'max_results'


def _walk_files(base: Path):
    """Yield files under ``base`` in deterministic order, skipping heavy dirs."""
    if base.is_file():
        yield base
        return
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for name in sorted(filenames):
            p = Path(dirpath) / name
            if p.is_file():
                yield p


def _head(verb: str, pattern: str, base_rel: str, count: int, unit: str) -> str:
    """Shared result header: ``<verb> '<pattern>' under <base_rel>: <count> <unit>``."""
    return f"{verb} '{pattern}' under {base_rel}: {count} {unit}"


def _truncation_marker(limit: int, noun: str) -> str:
    """Shared 'showing first N' truncation marker (noun is ``matches`` or ``files``)."""
    return f"[showing first {limit} {noun}; raise max_results to see more]"


def build_grep_tool(cfg: AppConfig) -> Tool:
    root = Path(cfg.root)

    def grep_files(args: dict) -> str:
        pattern = (args.get("pattern") or "").strip()
        if not pattern:
            raise GrepError("pattern must be a non-empty regex")
        try:
            flags = re.IGNORECASE if args.get("case_insensitive") else 0
            rx = re.compile(pattern, flags)
        except re.error as e:
            raise GrepError(f"invalid regex '{pattern}': {e}") from e

        limit = args.get("max_results")
        if limit is None:
            limit = 50
        limit = min(limit, MAX_RESULTS_CAP)
        before = args.get("before") or 0
        after = args.get("after") or 0
        files_only = bool(args.get("files_only"))
        include = args.get("include")
        exclude = args.get("exclude")

        path = args.get("path")
        if path is None or path == "":
            base = root
        else:
            base = _resolve(root, path)
        if not base.exists():
            raise GrepError(f"path not found: {path}")

        base_rel = base.relative_to(root).as_posix() or "."

        def _name_ok(name: str) -> bool:
            if include and not fnmatch.fnmatch(name, include):
                return False
            if exclude and fnmatch.fnmatch(name, exclude):
                return False
            return True

        output: list[str] = []
        match_count = 0
        files_seen = 0
        long_line = 0

        for f in _walk_files(base):
            if not _name_ok(f.name):
                continue
            try:
                if f.stat().st_size > MAX_FILE_BYTES:
                    continue
            except OSError:
                continue
            files_seen += 1
            try:
                with f.open("r", encoding="utf-8", errors="replace") as fh:
                    all_lines = fh.readlines()
            except (OSError, UnicodeError):
                continue

            match_lines: list[int] = []
            for lineno, line in enumerate(all_lines, 1):
                if "\x00" in line:
                    break
                if rx.search(line):
                    match_lines.append(lineno)

            if not match_lines:
                continue

            rel = f.relative_to(root).as_posix()

            if files_only:
                output.append(rel)
                match_count += 1
                if match_count >= limit:
                    break
                continue

            for idx, ml in enumerate(match_lines):
                start = max(1, ml - before)
                end = min(len(all_lines), ml + after)
                for ln in range(start, end + 1):
                    line = all_lines[ln - 1].rstrip()
                    if len(line) > MAX_LINE_CHARS:
                        line = line[:MAX_LINE_CHARS] + "..."
                        long_line = 1
                    if ln == ml:
                        output.append(f"{rel}:{ln}: {line}")
                    else:
                        output.append(f"{rel}:{ln}- {line}")
                match_count += 1
                if idx < len(match_lines) - 1:
                    output.append("--")
                if match_count >= limit:
                    break
            if match_count >= limit:
                break

        unit = "file(s)" if files_only else "match(es)"
        head = _head("grep", pattern, base_rel, match_count, unit)
        lines = [head, *output]
        if match_count >= limit:
            lines.append(_truncation_marker(limit, "matches"))
        if long_line:
            lines.append("[long lines truncated]")
        if not output:
            lines.append(f"(no matches in {files_seen} file(s))")
        return "\n".join(lines)

    return Tool(
        name="grep_files",
        description=(
            "Search project files for lines matching a Python regular expression "
            "(case-sensitive by default). 'path' is a relative directory or single "
            "file (default: project root). Returns 'file:line: content' per match "
            "(indentation preserved), capped at 'max_results' (default 50, max 200). "
            "Use 'before'/'after' for context lines, 'include'/'exclude' for filename "
            "glob filters, 'files_only' to list matching file paths. "
            "Skips binary files, files over 128KB, and heavy dirs."
        ),
        parameters={
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Python regex to search for"},
                "path": {
                    "type": "string",
                    "description": "relative directory (or file) to search, default '.'",
                },
                "case_insensitive": {
                    "type": "boolean",
                    "description": "match case-insensitively (default false)",
                },
                "max_results": {
                    "type": "integer",
                    "description": "max matching lines to return (default 50, max 200)",
                },
                "before": {
                    "type": "integer",
                    "description": "context lines before each match (default 0)",
                },
                "after": {
                    "type": "integer",
                    "description": "context lines after each match (default 0)",
                },
                "include": {
                    "type": "string",
                    "description": "filename glob to include (e.g. '*.py')",
                },
                "exclude": {
                    "type": "string",
                    "description": "filename glob to exclude (e.g. '*.md')",
                },
                "files_only": {
                    "type": "boolean",
                    "description": "return only matching file paths, not line content",
                },
            },
            "required": ["pattern"],
        },
        handler=grep_files,
    )


def build_find_tool(cfg: AppConfig) -> Tool:
    root = Path(cfg.root)

    def find_files(args: dict) -> str:
        pattern = (args.get("pattern") or "").strip()
        if not pattern:
            raise GrepError("pattern must be a non-empty glob")

        limit = args.get("max_results") or 100
        limit = min(limit, 500)

        path = args.get("path")
        if path is None or path == "":
            base = root
        else:
            base = _resolve(root, path)
        if not base.exists():
            raise GrepError(f"path not found: {path}")

        base_rel = base.relative_to(root).as_posix() or "."

        results: list[str] = []
        for f in _walk_files(base):
            rel = f.relative_to(root).as_posix()
            if fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(f.name, pattern):
                results.append(rel)
                if len(results) >= limit:
                    break

        head = _head("find", pattern, base_rel, len(results), "file(s)")
        lines = [head, *results]
        if len(results) >= limit:
            lines.append(_truncation_marker(limit, "files"))
        if not results:
            lines.append("(no files matched)")
        return "\n".join(lines)

    return Tool(
        name="find_files",
        description=(
            "Find files by glob pattern in the project workspace. "
            "Matches against both the filename and the relative path. "
            "Pattern examples: '*.py', '*.md', 'src/*.py'. "
            "Skips heavy dirs (.git, venv, node_modules, __pycache__)."
        ),
        parameters={
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "glob pattern to match filenames or relative paths",
                },
                "path": {
                    "type": "string",
                    "description": "relative directory to search, default '.'",
                },
                "max_results": {
                    "type": "integer",
                    "description": "max files to return (default 100, max 500)",
                },
            },
            "required": ["pattern"],
        },
        handler=find_files,
    )
