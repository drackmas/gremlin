"""Chat loop: event ordering, tool loop, history conversion, errors."""

from __future__ import annotations

import json
import threading

import pytest

from chat.manager import ChatManager
from models.base import ModelBackend, ModelEvent, ModelError
from sessions import SessionManager
from skills.loader import SkillLoader
from memory.store import MemoryStore
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
    assert events[-1]["stop_reason"] == "completed"
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
    stored = sessions.get(s["id"])["messages"][1]
    assert stored["timeline"] == [
        {"t": "thinking", "text": "hmm..."},
        {"t": "text", "text": "answer"},
    ]
    assert "thinking" not in stored

    # show_thinking off: no thinking events streamed, nothing persisted
    backend2 = FakeBackend(
        [[ModelEvent("thinking", text="hmm"), ModelEvent("text", text="answer"), ModelEvent("done")]]
    )
    sessions2 = SessionManager(cfg)
    manager2 = ChatManager(cfg, sessions2, build_registry(cfg), SkillLoader(cfg), backend2)
    s2 = sessions2.create("t")
    events2 = list(manager2.run(s2["id"], "hi", {**SETTINGS, "show_thinking": False}))
    assert all(e["type"] != "thinking" for e in events2)
    stored2 = sessions2.get(s2["id"])["messages"][1]
    assert "thinking" not in stored2
    assert stored2["timeline"] == [{"t": "text", "text": "answer"}]


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
    # Phase 1b precise pins: exactly 2 backend calls, and the last streamed text is
    # the response-2 text (the persisted assistant content concatenates both texts,
    # asserted below as "Let me read it.It says hi.").
    assert len(backend.calls) == 2
    assert events[2]["text"] == "It says hi."

    # the second model call must carry the tool result in OpenAI shape
    second = backend.calls[1]["messages"]
    roles = [m["role"] for m in second]
    assert "tool" in roles
    tool_msg = next(m for m in second if m["role"] == "tool")
    assert tool_msg["tool_call_id"] == "call_1"
    assert "hi from file" in tool_msg["content"]
    # the role:tool message carries the tool's actual returned string verbatim
    assert tool_msg["content"] == "hi from file"
    assistant_msg = next(m for m in second if m["role"] == "assistant" and m.get("tool_calls"))
    assert assistant_msg["tool_calls"][0]["function"]["name"] == "read_file"

    # stored assistant message keeps the tool call
    stored = sessions.get(s["id"])["messages"][1]
    assert stored["content"] == "Let me read it.It says hi."
    assert stored["tool_calls"][0]["name"] == "read_file"
    assert stored["tool_calls"][0]["result"].startswith("hi from file")
    assert stored["timeline"] == [
        {"t": "text", "text": "Let me read it."},
        {"t": "tool", "name": "read_file", "status": "ok"},
        {"t": "text", "text": "It says hi."},
    ]


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
    assert events[-1]["stop_reason"] == "model_error"
    err = next(e for e in events if e["type"] == "error")
    assert "model exploded" in err["message"]
    stored = sessions.get(s["id"])["messages"][1]
    assert "error" in stored["content"]
    last = stored["timeline"][-1]
    assert last["t"] == "text"
    assert last["text"].startswith("(error:")
    assert "model exploded" in last["text"]


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
    assert events[-1]["stop_reason"] == "max_iterations"
    # bounded by _DEFAULT_TOOL_ITERATIONS
    assert len(backend.calls) == cfg._DEFAULT_TOOL_ITERATIONS
    stored = sessions.get(s["id"])["messages"][1]
    last = stored["timeline"][-1]
    assert last["t"] == "text"
    assert "maximum iterations" in last["text"]


def test_timeline_interleaved_persisted(cfg):
    (cfg.root / "hello.txt").write_text("hi from file")
    backend = FakeBackend(
        [
            [
                ModelEvent("thinking", text="plan"),
                ModelEvent("text", text="Let me check."),
                ModelEvent("tool_call", tool_call_id="call_1", name="read_file", arguments={"path": "hello.txt"}),
                ModelEvent("done", finish_reason="tool_calls"),
            ],
            [
                ModelEvent("thinking", text="now answer"),
                ModelEvent("text", text="It says hi."),
                ModelEvent("done"),
            ],
        ]
    )
    sessions, manager = make_manager(cfg, backend)
    s = sessions.create("t")
    events = list(manager.run(s["id"], "read hello.txt", SETTINGS))
    assert [e["type"] for e in events] == [
        "thinking", "text", "tool_call", "thinking", "text", "done",
    ]
    stored = sessions.get(s["id"])["messages"][1]
    assert stored["timeline"] == [
        {"t": "thinking", "text": "plan"},
        {"t": "text", "text": "Let me check."},
        {"t": "tool", "name": "read_file", "status": "ok"},
        {"t": "thinking", "text": "now answer"},
        {"t": "text", "text": "It says hi."},
    ]
    assert stored["content"] == "Let me check.It says hi."
    assert stored["tool_calls"][0]["name"] == "read_file"
    assert stored["tool_calls"][0]["result"].startswith("hi from file")


def test_multiple_tools_one_response(cfg):
    (cfg.root / "hello.txt").write_text("hi")
    backend = FakeBackend(
        [
            [
                ModelEvent("tool_call", tool_call_id="c1", name="list_directory", arguments={}),
                ModelEvent("tool_call", tool_call_id="c2", name="read_file", arguments={"path": "hello.txt"}),
                ModelEvent("done", finish_reason="tool_calls"),
            ],
            [ModelEvent("text", text="done"), ModelEvent("done")],
        ]
    )
    sessions, manager = make_manager(cfg, backend)
    s = sessions.create("t")
    events = list(manager.run(s["id"], "go", SETTINGS))
    assert [e["type"] for e in events] == ["tool_call", "tool_call", "text", "done"]
    stored = sessions.get(s["id"])["messages"][1]
    assert stored["timeline"] == [
        {"t": "tool", "name": "list_directory", "status": "ok"},
        {"t": "tool", "name": "read_file", "status": "ok"},
        {"t": "text", "text": "done"},
    ]
    assert len(stored["tool_calls"]) == 2


def test_system_prompt_identity_and_time(cfg):
    backend = FakeBackend([[ModelEvent("text", text="hi"), ModelEvent("done")]])
    sessions, manager = make_manager(cfg, backend)
    s = sessions.create("t")
    list(manager.run(s["id"], "hello", {**SETTINGS, "identity": "You are a pirate."}))
    system = backend.calls[0]["messages"][0]["content"]
    assert "You are a pirate." in system
    assert "# Time" in system
    assert "Current time:" in system


def test_system_prompt_no_identity_section_when_empty(cfg):
    backend = FakeBackend([[ModelEvent("text", text="hi"), ModelEvent("done")]])
    sessions, manager = make_manager(cfg, backend)
    s = sessions.create("t")
    list(manager.run(s["id"], "hello", SETTINGS))
    system = backend.calls[0]["messages"][0]["content"]
    assert "# Identity" not in system


def test_compress_cuts_history(cfg):
    backend = FakeBackend(
        [
            [ModelEvent("text", text="Summary: user loves tea."), ModelEvent("done")],  # compress
            [ModelEvent("text", text="ok"), ModelEvent("done")],                        # next turn
        ]
    )
    sessions, manager = make_manager(cfg, backend)
    s = sessions.create("t")
    sessions.add_message(s["id"], {"id": "u1", "role": "user", "content": "I love tea", "ts": "t"})
    sessions.add_message(s["id"], {"id": "a1", "role": "assistant", "content": "Noted.", "ts": "t"})

    msg = manager.compress(s["id"], SETTINGS)
    assert msg["compression"] is True
    assert sessions.get(s["id"])["messages"][-1]["compression"] is True

    list(manager.run(s["id"], "more tea?", SETTINGS))
    sent = backend.calls[1]["messages"]
    assert sent[0]["role"] == "system"
    # Summary is now a system message with a prefix, not a raw user message.
    assert sent[1]["role"] == "system"
    assert "Summary: user loves tea." in sent[1]["content"]
    assert "[Conversation Summary]" in sent[1]["content"]
    # Pre-compression messages are not re-sent.
    assert all(m.get("content") != "I love tea" for m in sent)
    assert sent[-1] == {"role": "user", "content": "more tea?"}


def test_compress_empty_session_raises(cfg):
    backend = FakeBackend([[]])
    sessions, manager = make_manager(cfg, backend)
    s = sessions.create("empty")
    with pytest.raises(ModelError):
        manager.compress(s["id"], SETTINGS)
def test_pinned_memory_in_prompt(cfg):
    from memory.store import MemoryStore

    backend = FakeBackend([[ModelEvent("text", text="ok"), ModelEvent("done")]])
    sessions, manager = make_manager(cfg, backend)
    store = MemoryStore(cfg.data_dir / "memory.json")
    store.add("always answer in English", pin=True)
    manager.memory = store
    s = sessions.create("t")
    list(manager.run(s["id"], "hi", SETTINGS))
    system = backend.calls[0]["messages"][0]["content"]
    assert "# Pinned memory" in system
    assert "always answer in English" in system


def test_parse_tool_args():
    from models.openai_compat import parse_tool_args

    assert parse_tool_args("") == {}
    assert parse_tool_args('{"path": "a.txt",}') == {"path": "a.txt"}
    assert parse_tool_args('{"n": 1') == {"n": 1}
    assert parse_tool_args("[1, 2]") == {"value": [1, 2]}
    assert parse_tool_args("null") == {"value": None}


def test_registry_rejects_raw_malformed_args(cfg):
    backend = FakeBackend([[]])
    _, manager = make_manager(cfg, backend)
    text, ok = manager.registry.execute("memory_add", {"_raw": '{"content": oops'})
    assert ok is False
    assert "malformed JSON arguments" in text
    assert "Retry" in text


def test_discord_wiring_injects_pinned_memory(cfg):
    """Regression for #4: the Discord surface must wire the shared MemoryStore
    into the registry and manager exactly like app.py/cli.py, so pinned
    memories reach the system prompt on a Discord turn."""
    memory = MemoryStore(cfg.data_dir / "memory.json")
    memory.add("user prefers terse answers", pin=True)
    backend = FakeBackend([[ModelEvent(kind="text", text="ok"), ModelEvent(kind="done")]])
    sessions = SessionManager(cfg)
    registry = build_registry(cfg, SkillLoader(cfg), memory_store=memory)
    manager = ChatManager(cfg, sessions, registry, SkillLoader(cfg), backend, memory=memory)
    sid = sessions.create("discord")["id"]
    for _ in manager.run(sid, "hi", SETTINGS):
        pass
    sys_msg = backend.calls[0]["messages"][0]
    assert sys_msg["role"] == "system"
    assert "user prefers terse answers" in sys_msg["content"]


class UsageBackend(ModelBackend):
    """Fake tokenizer (one token per character of message content): each
    `done` carries the exact prompt size of the request Gremlin sent, so
    per-session usage attribution is checkable end to end."""

    def __init__(self):
        self.calls = []

    def stream(self, messages, tools, model):
        self.calls.append(json.loads(json.dumps(messages)))
        prompt_tokens = sum(len(str(m.get("content") or "")) for m in messages)
        yield ModelEvent("text", text="ok")
        yield ModelEvent(
            "done",
            usage={"prompt_tokens": prompt_tokens, "completion_tokens": 1, "total_tokens": prompt_tokens + 1},
        )


def test_usage_event_emitted_and_persisted(cfg):
    """A successful turn yields exactly one `usage` event (after the text,
    before `done`) and persists it as the session's `last_usage`."""
    backend = UsageBackend()
    sessions, manager = make_manager(cfg, backend)
    s = sessions.create()
    events = list(manager.run(s["id"], "hello world", SETTINGS))

    usage_events = [e for e in events if e["type"] == "usage"]
    assert len(usage_events) == 1
    u = usage_events[0]
    expected = sum(len(str(m.get("content") or "")) for m in backend.calls[0])
    assert u["session_id"] == s["id"]
    assert u["prompt_tokens"] == expected
    assert events[-1] == {"type": "done", "stop_reason": "completed"}
    assert events[-2] is u
    assert sessions.get(s["id"])["last_usage"]["prompt_tokens"] == expected


def test_last_usage_is_final_inference_not_max(cfg):
    """Definition check: `last_usage` is the usage of the FINAL model request
    of the turn, not the largest across the tool loop — a later, smaller
    request must overwrite an earlier, larger one."""

    class ShrinkingUsage(ModelBackend):
        def __init__(self):
            self.n = 0

        def stream(self, messages, tools, model):
            self.n += 1
            if self.n == 1:
                yield ModelEvent("tool_call", tool_call_id="c1", name="list_directory", arguments={})
                yield ModelEvent("done", usage={"prompt_tokens": 100, "completion_tokens": 1, "total_tokens": 101})
            else:
                yield ModelEvent("text", text="done")
                yield ModelEvent("done", usage={"prompt_tokens": 40, "completion_tokens": 1, "total_tokens": 41})

    backend = ShrinkingUsage()
    sessions, manager = make_manager(cfg, backend)
    s = sessions.create()
    events = list(manager.run(s["id"], "hi", SETTINGS))
    u = [e for e in events if e["type"] == "usage"][0]
    assert u["prompt_tokens"] == 40
    assert sessions.get(s["id"])["last_usage"]["prompt_tokens"] == 40


def test_session_isolation_a_b_a(cfg):
    """Application-level isolation: every request is built solely from the
    selected session's stored history. B's request contains none of A's
    content (and vice versa), A's thinking is never re-sent, each session's
    persisted last_usage is exactly its own final request, and the on-disk
    session files never contain the other session's content."""
    (cfg.root / "hello.txt").write_text("ALPHA-TOOL-RESULT")

    class IsoBackend(ModelBackend):
        def __init__(self):
            self.calls = []
            self.n = 0

        def stream(self, messages, tools, model):
            self.n += 1
            self.calls.append(json.loads(json.dumps(messages)))
            prompt_tokens = sum(len(str(m.get("content") or "")) for m in messages)
            usage = {"prompt_tokens": prompt_tokens, "completion_tokens": 1, "total_tokens": prompt_tokens + 1}
            if self.n == 3:  # A turn 2, first request: call a tool
                yield ModelEvent("tool_call", tool_call_id="t1", name="read_file", arguments={"path": "hello.txt"})
            else:
                if self.n == 1:
                    yield ModelEvent("thinking", text="THINK-A-SECRET")
                yield ModelEvent("text", text="ok")
            yield ModelEvent("done", usage=usage)

    backend = IsoBackend()
    sessions, manager = make_manager(cfg, backend)
    a = sessions.create()
    b = sessions.create()

    list(manager.run(a["id"], "ALPHA-A-1", SETTINGS))  # call 1: A turn 1 (thinking)
    list(manager.run(b["id"], "BRAVO-B-22", SETTINGS))  # call 2: B turn 1
    list(manager.run(a["id"], "ALPHA-A-2", SETTINGS))  # calls 3+4: A turn 2 (tool + final)

    calls = backend.calls
    assert len(calls) == 4
    a_t2_final = json.dumps(calls[3])
    assert "ALPHA-A-1" in a_t2_final and "ALPHA-A-2" in a_t2_final
    assert "ALPHA-TOOL-RESULT" in a_t2_final  # tool result re-sent within A
    assert "BRAVO" not in a_t2_final
    b_call = json.dumps(calls[1])
    assert "BRAVO-B-22" in b_call
    assert "ALPHA" not in b_call
    for later in calls[1:]:  # thinking never re-sent in any subsequent request
        assert "THINK-A-SECRET" not in json.dumps(later)

    # Per-session last_usage: exactly each session's own final request size.
    expected_a = sum(len(str(m.get("content") or "")) for m in calls[3])
    expected_b = sum(len(str(m.get("content") or "")) for m in calls[1])
    assert expected_a != expected_b
    assert sessions.get(a["id"])["last_usage"]["prompt_tokens"] == expected_a
    assert sessions.get(b["id"])["last_usage"]["prompt_tokens"] == expected_b

    # On-disk isolation: session files never hold the other session's content.
    a_raw = sessions._path(a["id"]).read_text()
    b_raw = sessions._path(b["id"]).read_text()
    assert "BRAVO" not in a_raw
    assert "ALPHA" not in b_raw
    assert "THINK-A-SECRET" not in b_raw


def test_failed_turn_does_not_emit_or_overwrite_usage(cfg):
    """A turn ending in model_error emits no `usage` event and leaves the
    previously persisted last_usage untouched."""

    class ErrBackend(ModelBackend):
        def stream(self, messages, tools, model):
            raise ModelError("boom")
            yield  # pragma: no cover

    backend = ErrBackend()
    sessions, manager = make_manager(cfg, backend)
    s = sessions.create()
    sessions.set_last_usage(s["id"], {"prompt_tokens": 999, "completion_tokens": 1, "total_tokens": 1000})
    events = list(manager.run(s["id"], "hi", SETTINGS))

    assert not [e for e in events if e["type"] == "usage"]
    assert any(e["type"] == "error" for e in events)
    assert events[-1]["type"] == "done"
    assert sessions.get(s["id"])["last_usage"]["prompt_tokens"] == 999

def test_new_turn_interrupts_previous_one(cfg):
    """A second run() on the same session supersedes the first.

    The first generator stops at its next event: no further text, no done,
    and its partial assistant reply is not saved. The interrupted user
    message stays in the session (the new turn needs it as context).
    """
    backend = FakeBackend(
        [
            [
                ModelEvent("text", text="one "),
                ModelEvent("text", text="two "),
                ModelEvent("text", text="three"),
                ModelEvent("done"),
            ],
            [ModelEvent("text", text="second"), ModelEvent("done")],
        ]
    )
    sessions, manager = make_manager(cfg, backend)
    s = sessions.create("t")

    gen = manager.run(s["id"], "first message", SETTINGS)
    first = [next(gen)]  # one event streamed, then the user sends a new message
    assert first == [{"type": "text", "text": "one "}]

    second = list(manager.run(s["id"], "second message", SETTINGS))
    assert [e["type"] for e in second] == ["text", "done"]
    assert second[0]["text"] == "second"

    # Resuming the superseded generator terminates cleanly at the cancel flag:
    # no "two ", no "three", no done, no exception.
    rest = list(gen)
    assert rest == []

    stored = sessions.get(s["id"])["messages"]
    assert [m["role"] for m in stored] == ["user", "user", "assistant"]
    assert [m["content"] for m in stored] == ["first message", "second message", "second"]


class _ScriptedToolRegistry:
    """Registry with per-tool behavior; records which tools executed.

    Optionally blocks on a named tool until ``release_event`` is set, so a
    test can interrupt while that tool is in flight.
    """

    def __init__(self, block_tool: str | None = None):
        self.block_tool = block_tool
        self.executed: list[str] = []
        self.started_event = threading.Event()
        self.release_event = threading.Event()

    def tools(self) -> list:
        return []

    def to_openai_tools(self) -> list[dict]:
        return []

    def execute(self, name: str, args) -> tuple[str, bool]:
        if name == self.block_tool:
            self.started_event.set()
            assert self.release_event.wait(timeout=5), "tool was never released"
        self.executed.append(name)
        return f"{name} done", True


class _RecordingStreamBackend(FakeBackend):
    """FakeBackend that records how many events each stream the loop pulled.

    ``pulled`` increments the moment an event is taken from the backend (before
    it is handed to the consumer), so it distinguishes "the loop pulled the
    next token but the turn was already superseded" from "the loop stopped
    pulling at the cancel check point".
    """

    def __init__(self, scripts):
        super().__init__(scripts)
        self.pulled_counts: dict[int, tuple[int, int]] = {}

    def stream(self, messages, tools, model):
        idx = len(self.calls)
        self.calls.append({"messages": json.loads(json.dumps(messages)), "tools": tools, "model": model})
        events = self.scripts.pop(0)
        pulled = 0
        try:
            for ev in events:
                pulled += 1  # counted when pulled from the backend
                yield ev
        finally:
            self.pulled_counts[idx] = (pulled, len(events))


def test_interrupt_during_tool_call(cfg):
    """An interrupt while a tool is running skips the rest of the batch.

    The in-flight tool runs to completion, but the tool calls queued behind
    it are never started, and the interrupted turn is not persisted.
    """
    backend = FakeBackend(
        [
            [
                ModelEvent("tool_call", tool_call_id="c1", name="tool_a", arguments={}),
                ModelEvent("tool_call", tool_call_id="c2", name="tool_b", arguments={}),
                ModelEvent("done"),
            ],
            [ModelEvent("text", text="second"), ModelEvent("done")],
        ]
    )
    sessions, manager = make_manager(cfg, backend)
    registry = _ScriptedToolRegistry(block_tool="tool_a")
    manager.registry = registry
    s = sessions.create("t")

    old_events: list[dict] = []
    gen = manager.run(s["id"], "first message", SETTINGS)

    def consume():
        old_events.extend(gen)

    t = threading.Thread(target=consume, daemon=True)
    t.start()
    assert registry.started_event.wait(timeout=5), "tool_a never started"

    # User interrupts while tool_a is still executing.
    second = list(manager.run(s["id"], "second message", SETTINGS))
    registry.release_event.set()
    t.join(timeout=5)
    assert not t.is_alive(), "interrupted turn did not terminate"

    # tool_b was never started; tool_a finished but its result was discarded.
    assert registry.executed == ["tool_a"]
    assert len(backend.calls) == 2, "interrupted turn made a further model call"

    # The new turn completed normally.
    assert [e["type"] for e in second] == ["text", "done"]
    assert second[0]["text"] == "second"

    # The interrupted turn delivered nothing (no text before the tools).
    assert old_events == []

    # No partial assistant reply from the interrupted turn.
    stored = sessions.get(s["id"])["messages"]
    assert [m["role"] for m in stored] == ["user", "user", "assistant"]
    assert [m["content"] for m in stored] == ["first message", "second message", "second"]


def test_interrupt_prevents_next_model_call(cfg):
    """After a tool round, an interrupted turn makes no further model calls.

    The cancel flag is checked at the top of each tool-loop iteration, so the
    next model request is never started for a superseded turn.
    """
    backend = FakeBackend(
        [
            [ModelEvent("tool_call", tool_call_id="c1", name="tool_a", arguments={}), ModelEvent("done")],
            [ModelEvent("text", text="second"), ModelEvent("done")],
            [ModelEvent("text", text="too late"), ModelEvent("done")],  # must never be consumed
        ]
    )
    sessions, manager = make_manager(cfg, backend)
    manager.registry = _ScriptedToolRegistry()  # tools complete immediately
    s = sessions.create("t")

    gen = manager.run(s["id"], "first message", SETTINGS)
    first_event = next(gen)  # the tool_call event (tool_a already ran)
    assert first_event["type"] == "tool_call"

    second = list(manager.run(s["id"], "second message", SETTINGS))
    rest = list(gen)  # superseded generator terminates cleanly
    assert rest == []

    assert [e["type"] for e in second] == ["text", "done"]
    assert second[0]["text"] == "second"
    assert len(backend.calls) == 2, "interrupted turn started a second model call"

    stored = sessions.get(s["id"])["messages"]
    assert [m["role"] for m in stored] == ["user", "user", "assistant"]
    assert stored[-1]["content"] == "second"


def test_interrupt_closes_inflight_stream(cfg):
    """Interrupting mid-stream closes the backend stream immediately.

    The per-event cancel check breaks out of the stream loop and the
    ``finally`` closes the generator, so the backend stops producing tokens at
    once instead of being abandoned to GC.
    """
    backend = _RecordingStreamBackend(
        [
            [
                ModelEvent("text", text="first "),
                ModelEvent("text", text="second "),
                ModelEvent("text", text="third"),
            ],
            [ModelEvent("text", text="second"), ModelEvent("done")],
        ]
    )
    sessions, manager = make_manager(cfg, backend)
    s = sessions.create("t")

    gen = manager.run(s["id"], "first message", SETTINGS)
    first_event = next(gen)
    assert first_event == {"type": "text", "text": "first "}

    second = list(manager.run(s["id"], "second message", SETTINGS))
    rest = list(gen)
    assert rest == []

    assert [e["type"] for e in second] == ["text", "done"]
    assert len(backend.calls) == 2
    # The interrupted turn's loop (call 0) pulled only the first event before
    # the cancel check point stopped it; the new turn's stream (call 1) ran to
    # completion.
    assert backend.pulled_counts[0] == (1, 3)
    assert backend.pulled_counts[1] == (2, 2)


# --- Stop button (cooperative abort without a new turn) ----------------------


def test_stop_aborts_active_turn(cfg):
    """Stop cancels the active generation without starting a new turn.

    Nothing is delivered after the abort (no partial reply, no ``done``),
    no further model calls are made, the session stays clean, and a
    subsequent turn proceeds normally.
    """
    backend = FakeBackend(
        [
            [ModelEvent("text", text="partial "), ModelEvent("text", text="text"), ModelEvent("done")],
            [ModelEvent("text", text="next turn"), ModelEvent("done")],  # must never be consumed by turn 1
        ]
    )
    sessions, manager = make_manager(cfg, backend)
    s = sessions.create("t")

    gen = manager.run(s["id"], "first message", SETTINGS)
    assert next(gen) == {"type": "text", "text": "partial "}

    # User taps Stop: cooperative cancel, no new turn.
    assert manager.abort(s["id"]) is True
    assert list(gen) == [], "aborted turn must deliver nothing further"
    assert len(backend.calls) == 1, "aborted turn started a second model call"
    assert manager._active_runs == {}, "run must be released"

    # No partial assistant reply saved; the user message stays.
    stored = sessions.get(s["id"])["messages"]
    assert [m["role"] for m in stored] == ["user"]

    # A subsequent turn proceeds cleanly.
    second = list(manager.run(s["id"], "second message", SETTINGS))
    assert [e["type"] for e in second] == ["text", "done"]
    stored = sessions.get(s["id"])["messages"]
    assert [m["role"] for m in stored] == ["user", "user", "assistant"]
    assert stored[-1]["content"] == "next turn"

    # Aborting again with nothing active is a no-op.
    assert manager.abort(s["id"]) is False


def test_stop_during_tool_call(cfg):
    """Stop while a tool is in flight: the tool finishes, its result is
    discarded, and the remaining tools / model calls are skipped."""
    backend = FakeBackend(
        [
            [ModelEvent("tool_call", tool_call_id="c1", name="tool_a", arguments={}), ModelEvent("done")],
            [ModelEvent("tool_call", tool_call_id="c2", name="tool_b", arguments={}), ModelEvent("done")],
        ]
    )
    sessions, manager = make_manager(cfg, backend)
    registry = _ScriptedToolRegistry(block_tool="tool_a")
    manager.registry = registry
    s = sessions.create("t")

    old_events: list[dict] = []
    gen = manager.run(s["id"], "first message", SETTINGS)

    def consume():
        old_events.extend(gen)

    t = threading.Thread(target=consume, daemon=True)
    t.start()
    assert registry.started_event.wait(timeout=5), "tool_a never started"

    # User taps Stop while tool_a is blocked.
    assert manager.abort(s["id"]) is True
    registry.release_event.set()
    t.join(timeout=5)
    assert not t.is_alive(), "aborted turn did not terminate"

    assert registry.executed == ["tool_a"], "tool_b must never be started"
    assert len(backend.calls) == 1
    assert old_events == [], "aborted turn delivered nothing"

    stored = sessions.get(s["id"])["messages"]
    assert [m["role"] for m in stored] == ["user"]


def test_stop_closes_inflight_stream(cfg):
    """Stop mid-stream closes the backend stream at the next checkpoint."""
    backend = _RecordingStreamBackend(
        [
            [
                ModelEvent("text", text="first "),
                ModelEvent("text", text="second "),
                ModelEvent("text", text="third"),
                ModelEvent("done"),
            ],
        ]
    )
    sessions, manager = make_manager(cfg, backend)
    s = sessions.create("t")

    gen = manager.run(s["id"], "hi", SETTINGS)
    assert next(gen) == {"type": "text", "text": "first "}
    assert manager.abort(s["id"]) is True
    assert list(gen) == []
    assert backend.pulled_counts[0] == (1, 4), "stream must stop at the cancel checkpoint"
    assert manager._active_runs == {}


def test_client_disconnect_aborts_turn(cfg):
    """Closing the generator (browser Stop / navigation) cancels the run and
    closes the backend stream; the session is left clean for the next turn."""
    backend = _RecordingStreamBackend(
        [
            [
                ModelEvent("text", text="a"),
                ModelEvent("text", text="b"),
                ModelEvent("text", text="c"),
                ModelEvent("done"),
            ],
            [ModelEvent("text", text="next"), ModelEvent("done")],
        ]
    )
    sessions, manager = make_manager(cfg, backend)
    s = sessions.create("t")

    gen = manager.run(s["id"], "hi", SETTINGS)
    assert next(gen) == {"type": "text", "text": "a"}
    gen.close()  # browser navigated away / fetch aborted

    assert manager._active_runs == {}, "run must be released on close"
    assert backend.pulled_counts[0] == (1, 4), "stream must stop at close"

    # Session not corrupted: the next turn is clean.
    second = list(manager.run(s["id"], "again", SETTINGS))
    assert [e["type"] for e in second] == ["text", "done"]
    stored = sessions.get(s["id"])["messages"]
    assert [m["role"] for m in stored] == ["user", "user", "assistant"]
    assert stored[-1]["content"] == "next"
