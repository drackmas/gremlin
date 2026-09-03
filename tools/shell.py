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
import subprocess

from .filesystem import SandboxError
from .registry import Tool

DEFAULT_TIMEOUT = 60
MIN_TIMEOUT = 1
MAX_TIMEOUT = 300
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


def build_shell_tool(cfg) -> Tool:
    root = os.path.realpath(cfg.root)

    def run_command(args: dict) -> str:
        command = (args.get("command") or "").strip()
        if not command:
            raise SandboxError("command must be a non-empty string")

        timeout = args.get("timeout")
        if timeout is None:
            timeout = DEFAULT_TIMEOUT
        if not isinstance(timeout, int) or isinstance(timeout, bool):
            raise SandboxError("timeout must be an integer number of seconds")
        timeout = max(MIN_TIMEOUT, min(timeout, MAX_TIMEOUT))

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

    return Tool(
        name="run_command",
        description=(
            "Run a shell command inside the project sandbox (working directory = project root) "
            "and return its exit code, stdout and stderr. Use it to run tests, build, grep, or "
            "execute a one-off script to verify your work. A non-zero exit code means the command "
            "failed - read stderr for why. Very long output is truncated."
        ),
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
