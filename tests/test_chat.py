"""Chat loop: event ordering, tool loop, history conversion, errors."""

from __future__ import annotations

import json

import pytest

from chat.manager import ChatManager
from models.base import ModelBackend, ModelEvent, ModelError
from sessions import SessionManager
from skills.loader import SkillLoader
from tools import build_registry


class FakeBackend(ModelBackend):
    """Scripted backend: each call pops the next list of events."""

    def __init__(self, scripts):
        self.scripts = scripts
        self.calls = []

    def stream(self, messages, tools, model):
        self.calls.append({"messages": json.loads(json.dumps(messages)), "tools": tools, "model": model})
        events = self.scripts.pop(0)
        for ev in events:
            yield ev


def make_manager(cfg, backend):
    sessions = SessionManager(cfg)
    registry = build_registry(cfg)
    manager = ChatManager(cfg, sessions, registry, SkillLoader(cfg), backend)
    return sessions, manager


SETTINGS = {"show_thinking": True, "base_url": "http://fake", "model": "fake"}


def test_plain_text_turn(cfg):
    backend = FakeBackend([[ModelEvent("text", text="Hello "), ModelEvent("text", text="there"), ModelEvent("done")]])
    sessions, manager = make_manager(cfg, backend)
    s = sessions.create("t")
    events = list(manager.run(s["id"], "hi", SETTINGS))
    types = [e["type"] for e in events]
    assert types == ["text", "text", "done"]
    stored = sessions.get(s["id"])["messages"]
    assert stored[0]["role"] == "user"
    assert stored[1]["role"] == "assistant"
    assert stored[1]["content"] == "Hello there"
    # system prompt first in API history
    assert backend.calls[0]["messages"][0]["role"] == "system"
    assert "Gremlin" in backend.calls[0]["messages"][0]["content"]


def test_thinking_captured_and_filtered(cfg):
    backend = FakeBackend(
        [
            [
                ModelEvent("thinking", text="hmm..."),
                ModelEvent("text", text="answer"),
                ModelEvent("done"),
            ]
        ]
    )
    sessions, manager = make_manager(cfg, backend)
    s = sessions.create("t")
    events = list(manager.run(s["id"], "hi", SETTINGS))
    assert [e["type"] for e in events] == ["thinking", "text", "done"]
    assert sessions.get(s["id"])["messages"][1]["thinking"] == "hmm..."

    # show_thinking off: no thinking events streamed, nothing persisted
    backend2 = FakeBackend(
        [[ModelEvent("thinking", text="hmm"), ModelEvent("text", text="answer"), ModelEvent("done")]]
    )
    sessions2 = SessionManager(cfg)
    manager2 = ChatManager(cfg, sessions2, build_registry(cfg), SkillLoader(cfg), backend2)
    s2 = sessions2.create("t")
    events2 = list(manager2.run(s2["id"], "hi", {**SETTINGS, "show_thinking": False}))
    assert all(e["type"] != "thinking" for e in events2)
    assert "thinking" not in sessions2.get(s2["id"])["messages"][1]


def test_tool_loop_end_to_end(cfg):
    (cfg.root / "hello.txt").write_text("hi from file")
    backend = FakeBackend(
        [
            [
                ModelEvent("text", text="Let me read it."),
                ModelEvent("tool_call", tool_call_id="call_1", name="read_file", arguments={"path": "hello.txt"}),
                ModelEvent("done", finish_reason="tool_calls"),
            ],
            [ModelEvent("text", text="It says hi."), ModelEvent("done")],
        ]
    )
    sessions, manager = make_manager(cfg, backend)
    s = sessions.create("t")
    events = list(manager.run(s["id"], "read hello.txt", SETTINGS))
    types = [e["type"] for e in events]
    assert types == ["text", "tool_call", "text", "done"]
    tc = events[1]
    assert tc["name"] == "read_file"
    assert tc["status"] == "ok"
    assert "hi from file" in tc["result"]

    # the second model call must carry the tool result in OpenAI shape
    second = backend.calls[1]["messages"]
    roles = [m["role"] for m in second]
    assert "tool" in roles
    tool_msg = next(m for m in second if m["role"] == "tool")
    assert tool_msg["tool_call_id"] == "call_1"
    assert "hi from file" in tool_msg["content"]
    assistant_msg = next(m for m in second if m["role"] == "assistant" and m.get("tool_calls"))
    assert assistant_msg["tool_calls"][0]["function"]["name"] == "read_file"

    # stored assistant message keeps the tool call
    stored = sessions.get(s["id"])["messages"][1]
    assert stored["content"] == "Let me read it.It says hi."
    assert stored["tool_calls"][0]["name"] == "read_file"
    assert stored["tool_calls"][0]["result"].startswith("hi from file")


def test_failing_tool_reported_to_model_and_client(cfg):
    backend = FakeBackend(
        [
            [
                ModelEvent("tool_call", tool_call_id="call_9", name="read_file", arguments={"path": "/etc/passwd"}),
                ModelEvent("done", finish_reason="tool_calls"),
            ],
            [ModelEvent("text", text="Cannot read that path."), ModelEvent("done")],
        ]
    )
    sessions, manager = make_manager(cfg, backend)
    s = sessions.create("t")
    events = list(manager.run(s["id"], "read /etc/passwd", SETTINGS))
    tc = next(e for e in events if e["type"] == "tool_call")
    assert tc["status"] == "error"
    assert tc["result"].startswith("ERROR")
    # model saw the error as the tool result
    second = backend.calls[1]["messages"]
    tool_msg = next(m for m in second if m["role"] == "tool")
    assert tool_msg["content"].startswith("ERROR")


def test_model_error_yields_error_event(cfg):
    class Boom(ModelBackend):
        def stream(self, messages, tools, model):
            raise ModelError("model exploded", 500)
            yield  # pragma: no cover

    sessions, manager = make_manager(cfg, Boom())
    s = sessions.create("t")
    events = list(manager.run(s["id"], "hi", SETTINGS))
    assert any(e["type"] == "error" for e in events)
    assert events[-1]["type"] == "done"
    err = next(e for e in events if e["type"] == "error")
    assert "model exploded" in err["message"]
    stored = sessions.get(s["id"])["messages"][1]
    assert "error" in stored["content"]


def test_tool_loop_exhaustion(cfg):
    backend = FakeBackend(
        [[ModelEvent("tool_call", tool_call_id=f"c{i}", name="list_directory", arguments={}), ModelEvent("done")] for i in range(20)]
    )
    sessions, manager = make_manager(cfg, backend)
    s = sessions.create("t")
    events = list(manager.run(s["id"], "loop forever", SETTINGS))
    err = next(e for e in events if e["type"] == "error")
    assert "maximum iterations" in err["message"]
    assert events[-1]["type"] == "done"
    # bounded by MAX_TOOL_ITERATIONS
    assert len(backend.calls) == cfg.MAX_TOOL_ITERATIONS
