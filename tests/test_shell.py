"""Sandboxed run_command tool: execution, exit codes, cwd, timeout, truncation."""

from __future__ import annotations

from tools import build_registry


def make_reg(cfg):
    return build_registry(cfg)


def test_run_command_registered(cfg):
    assert "run_command" in build_registry(cfg).names()


def test_run_command_success(cfg):
    out, ok = make_reg(cfg).execute("run_command", {"command": "echo hello"})
    assert ok is True
    assert "exit code: 0" in out
    assert "hello" in out


def test_run_command_nonzero_exit_reported(cfg):
    # A non-zero exit is a successful *tool run*; the exit code is reported for
    # the model to react to (not surfaced as a tool-level ok=False).
    out, ok = make_reg(cfg).execute("run_command", {"command": "exit 3"})
    assert ok is True
    assert "exit code: 3" in out


def test_run_command_captures_stderr(cfg):
    out, ok = make_reg(cfg).execute("run_command", {"command": "echo oops 1>&2; exit 1"})
    assert ok is True
    assert "stderr:" in out
    assert "oops" in out
    assert "exit code: 1" in out


def test_run_command_cwd_is_sandbox_root(cfg):
    out, ok = make_reg(cfg).execute("run_command", {"command": "pwd"})
    assert ok is True
    assert str(cfg.root.resolve()) in out


def test_run_command_can_read_sandbox_file(cfg):
    (cfg.root / "note.txt").write_text("secret inside")
    out, ok = make_reg(cfg).execute("run_command", {"command": "cat note.txt"})
    assert ok is True
    assert "secret inside" in out


def test_run_command_empty_command_rejected(cfg):
    out, ok = make_reg(cfg).execute("run_command", {"command": "   "})
    assert ok is False
    assert "ERROR" in out


def test_run_command_bad_timeout_rejected(cfg):
    out, ok = make_reg(cfg).execute("run_command", {"command": "echo x", "timeout": "fast"})
    assert ok is False
    assert "ERROR" in out


def test_run_command_timeout(cfg):
    out, ok = make_reg(cfg).execute("run_command", {"command": "sleep 3", "timeout": 1})
    assert ok is False
    assert "timed out" in out


def test_run_command_truncates_long_output(cfg):
    out, ok = make_reg(cfg).execute(
        "run_command", {"command": "python3 -c \"import sys; sys.stdout.write('x'*300000)\""}
    )
    assert ok is True
    assert "[truncated" in out
