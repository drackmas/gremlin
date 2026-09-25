"""Sandboxed command execution.

``run_command`` runs a shell command from the project root (the sandbox) and
returns its exit code, stdout and stderr. It is the model's verification and
build capability: run tests, grep, compile, or execute a one-off script to
check that a change actually works before declaring the task done.

Bounds that keep a single command from derailing the agent:
  * the working directory is the sandbox root, never the caller's home;
  * a wall-clock timeout (clamped to 1..300s) kills runaway commands;
  * stdout/stderr are truncated so one command cannot flood the context.

This is a project-scoped convenience layered on the existing path sandbox; it
is not a hard OS isolation boundary (see the design report for the tradeoff).
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess

from .filesystem import SandboxError
from .registry import Tool

from config import AppConfig
from collections.abc import Callable

DEFAULT_TIMEOUT = 60
MIN_TIMEOUT = 1
MAX_TIMEOUT = 300

_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_OP_CHARS = set("();|&{}<>")


def command_programs(command: str) -> list[str] | None:
    """Extract the leading program of every simple command in *command*.

    Handles pipes, sequences, backgrounding, and subshells, skipping
    ``VAR=value`` env assignments. Returns ``None`` if the command cannot be
    lexed (unclosed quote, etc.) — callers treat that as "unverifiable".
    """
    try:
        lex = shlex.shlex(command, posix=True, punctuation_chars="();|&{}<>")
        lex.whitespace_split = True
        tokens = list(lex)
    except ValueError:
        return None
    programs: list[str] = []
    need = True
    for tok in tokens:
        if tok and set(tok) <= _OP_CHARS:
            need = True
            continue
        if need:
            if _ASSIGN_RE.match(tok):
                continue
            programs.append(os.path.basename(tok))
            need = False
    return programs
OUTPUT_LIMIT = 20000


def _truncate(text: str | None, limit: int = OUTPUT_LIMIT) -> str:
    """Trim long output, keeping the head and the tail (where errors land)."""
    if not text:
        return ""
    if len(text) <= limit:
        return text
    head = int(limit * 0.6)
    tail = limit - head
    cut = len(text) - head - tail
    return f"{text[:head]}\n...[truncated {cut} chars]...\n{text[-tail:]}"


def _normalize_allowlist(raw) -> tuple[str, ...]:
    """Coerce a settings/env allow-list into lowercase program names.

    Accepts a comma string or a list/tuple of strings; blank entries are
    dropped. Anything else (or empty input) yields an empty tuple, i.e.
    unrestricted.
    """
    if isinstance(raw, str):
        items = raw.split(",")
    elif isinstance(raw, (list, tuple)):
        items = [str(x) for x in raw]
    else:
        return ()
    return tuple(x.strip().lower() for x in items if str(x).strip())


def _resolve_allowlist(settings_loader, cfg: AppConfig) -> tuple[str, ...]:
    """The current shell program allow-list (empty tuple = unrestricted).

    ``settings.json`` (``shell_allowlist``) is the source of truth when a
    loader is wired; the ``GREMLIN_SHELL_ALLOWLIST`` env (``cfg.shell_allowlist``)
    is the fallback for front doors without a settings store.
    """
    raw = None
    if settings_loader is not None:
        try:
            raw = settings_loader().get("shell_allowlist")
        except Exception:
            raw = None
    if not raw:
        raw = getattr(cfg, "shell_allowlist", ()) or ()
    return _normalize_allowlist(raw)


def build_shell_tool(cfg: AppConfig, settings_loader: Callable[[], dict] | None = None) -> Tool:
    root = os.path.realpath(cfg.root)
    build_allowlist = _resolve_allowlist(settings_loader, cfg)

    def run_command(args: dict) -> str:
        command = (args.get("command") or "").strip()
        if not command:
            raise SandboxError("command must be a non-empty string")

        timeout = args.get("timeout")
        if timeout is None:
            timeout = DEFAULT_TIMEOUT
        if not isinstance(timeout, int) or isinstance(timeout, bool):
            raise SandboxError("timeout must be an integer number of seconds")
        allowlist = _resolve_allowlist(settings_loader, cfg)
        timeout = max(MIN_TIMEOUT, min(timeout, MAX_TIMEOUT))
        if allowlist:
            allowed = {p.lower() for p in allowlist}
            progs = command_programs(command)
            if progs is None:
                raise SandboxError(
                    "shell is in restricted mode (allow-list active) and this command "
                    "could not be parsed; refusing to run it."
                )
            for p in progs:
                if p.lower() not in allowed:
                    raise SandboxError(
                        f"program {p!r} is not in the shell allow-list. "
                        f"Allowed programs: {', '.join(sorted(allowed))}"
                    )

        try:
            proc = subprocess.run(
                ["/bin/sh", "-c", command],
                cwd=root,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as e:
            partial = ""
            for stream in (e.stdout, e.stderr):
                if stream:
                    partial += stream.decode() if isinstance(stream, bytes) else stream
            partial = _truncate(partial)
            raise SandboxError(
                f"command timed out after {timeout}s and was killed."
                + (f"\nPartial output:\n{partial}" if partial else "\nNo output before timeout.")
            ) from None
        except OSError as e:
            raise SandboxError(f"cannot run command: {e}") from e

        stdout = _truncate(proc.stdout)
        stderr = _truncate(proc.stderr)
        lines = [f"exit code: {proc.returncode}"]
        if stdout:
            lines.append(f"stdout:\n{stdout}")
        if stderr:
            lines.append(f"stderr:\n{stderr}")
        if not stdout and not stderr:
            lines.append("(no output)")
        return "\n\n".join(lines)
    if build_allowlist:
        description = (
            "Run a shell command inside the project sandbox (working directory = project root) "
            "and return its exit code, stdout and stderr. RESTRICTED: only these programs are "
            f"allowed: {', '.join(sorted(build_allowlist))}. Piped/sequenced commands must also use only "
            "allowed programs."
        )
    else:
        description = (
            "Run a shell command inside the project sandbox (working directory = project root) "
            "and return its exit code, stdout and stderr. Use it to run tests, build, grep, or "
            "execute a one-off script to verify your work. A non-zero exit code means the command "
            "failed - read stderr for why. Very long output is truncated."
        )

    return Tool(
        name="run_command",
        description=description,
        parameters={
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "shell command to run (e.g. 'python -m pytest -q')",
                },
                "timeout": {
                    "type": "integer",
                    "description": f"max seconds to wait, default {DEFAULT_TIMEOUT}, clamped to {MIN_TIMEOUT}..{MAX_TIMEOUT}",
                },
            },
            "required": ["command"],
        },
        handler=run_command,
    )
