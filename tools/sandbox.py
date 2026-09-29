"""Sandbox path resolution: the shared boundary for all file tools.

Every tool that touches the filesystem must resolve model-supplied paths
through :func:`_resolve`. Paths are relative-only, normalized, resolved
through symlinks, and verified to stay inside the sandbox root. Enforcement
lives here in code, not in the prompt.
"""

from __future__ import annotations

import os
from pathlib import Path


from .registry import ToolExecutionError


class SandboxError(ToolExecutionError):
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
    """Validate that *name* is a safe bare filename. Returns it unchanged."""
    if not isinstance(name, str) or not name.strip():
        raise SandboxError("name must be a non-empty string")
    if os.path.isabs(name) or "/" in name or (os.sep != "/" and "\\" in name):
        raise SandboxError(f"not a bare filename: {name}")
    if name in (".", ".."):
        raise SandboxError(f"invalid filename: {name}")
    return name
