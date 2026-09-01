"""CLI REPL: command parsing + scripted turns via a fake backend."""

from __future__ import annotations

import pytest

import cli
from models.base import ModelBackend, ModelError, ModelEvent
from sessions import SessionManager


class FakeBackend(ModelBackend):
    def __init__(self, scripts):
        self.scripts = scripts
        self.calls = []

    def stream(self, messages, tools, model):
        self.calls.append({"messages": messages})
        for ev in self.scripts.pop(0):
            yield ev


def test_handle_command_quit():
    assert cli._handle_command("/quit", None) == ("quit", None)
    assert cli._handle_command("/exit", None)[0] == "quit"


def test_handle_command_help():
    action, detail = cli._handle_command("/help", None)
    assert action == "help" and "/new" in detail


def test_handle_command_unknown():
    action, detail = cli._handle_command("/frobnicate x", None)
    assert action == "unknown" and detail == "/frobnicate"


def test_handle_command_new(cfg):
    sessions = SessionManager(cfg)
    action, sid = cli._handle_command("/new", sessions)
    assert action == "new" and sessions.get(sid)["title"] == "terminal"


def test_main_full_turn(cfg, monkeypatch, capsys):
    backend = FakeBackend([[ModelEvent(kind="text", text="hi there"), ModelEvent(kind="done")]])
    inputs = iter(["hello", "/quit"])
    monkeypatch.setattr("builtins.input", lambda *a: next(inputs, ""))
    rc = cli.main(cfg=cfg, backend=backend)
    out = capsys.readouterr().out
    assert rc == 0
    assert "hi there" in out
    assert "8 chars" in out  # status line
    assert backend.calls and backend.calls[0]["messages"][-1]["content"] == "hello"


def test_main_new_and_quit(cfg, monkeypatch, capsys):
    inputs = iter(["/new", "/quit"])
    monkeypatch.setattr("builtins.input", lambda *a: next(inputs, ""))
    rc = cli.main(cfg=cfg)
    out = capsys.readouterr().out
    assert rc == 0
    assert "new session" in out


def test_main_error_line(cfg, monkeypatch, capsys):
    class Boom(ModelBackend):
        def stream(self, messages, tools, model):
            raise ModelError("kaboom")

    inputs = iter(["hello", "/quit"])
    monkeypatch.setattr("builtins.input", lambda *a: next(inputs, ""))
    rc = cli.main(cfg=cfg, backend=Boom())
    out = capsys.readouterr().out
    assert rc == 0
    assert "error:" in out
