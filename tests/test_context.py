"""Context management: bounded builder, auto-compaction, tool result protection."""

from __future__ import annotations

import pytest

from chat.manager import (
    ChatManager,
    _estimate_messages_tokens,
    _estimate_tokens,
    _find_last_compression,
    _group_into_turns,
    _truncate_tool_result,
)
from models.base import ModelBackend, ModelEvent, ModelError
from sessions import SessionManager
from skills.loader import SkillLoader
from tools import build_registry


# ---------------------------------------------------------------------------
# Fixtures & helpers
# ---------------------------------------------------------------------------

SETTINGS = {
    "base_url": "http://fake/v1",
    "model": "m",
    "show_thinking": False,
    "max_context_tokens": 200,  # small for testing
    "context_window_turns": 2,
    "compaction_threshold": 0.6,
    "tool_result_max_chars": 100,
}


class FakeBackend(ModelBackend):
    def __init__(self, responses: list[list[ModelEvent]]):
        self.responses = responses
        self.calls: list[dict] = []

    def stream(self, messages, tools, model):
        self.calls.append({
            "messages": messages,
            "tools": tools,
            "model": model,
        })
        if self.responses:
            evs = self.responses.pop(0)
        else:
            evs = [ModelEvent("text", text="ok"), ModelEvent("done")]
        yield from evs


def make_manager(cfg, backend=None):
    backend = backend or FakeBackend([[]])
    sessions = SessionManager(cfg)
    registry = build_registry(cfg, SkillLoader(cfg))
    manager = ChatManager(cfg, sessions, registry, SkillLoader(cfg), backend)
    return sessions, manager, backend


def _add_turn(sessions, sid, user_text, assistant_text, tool_calls=None):
    """Add one user+assistant turn to a session."""
    sessions.add_message(sid, {"id": f"u_{user_text}", "role": "user", "content": user_text, "ts": "t"})
    msg = {"id": f"a_{user_text}", "role": "assistant", "content": assistant_text, "ts": "t"}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    sessions.add_message(sid, msg)


# ---------------------------------------------------------------------------
# Helper function tests
# ---------------------------------------------------------------------------


class TestEstimateTokens:
    def test_empty_string(self):
        assert _estimate_tokens("") == 1  # minimum 1

    def test_short_string(self):
        assert _estimate_tokens("hello") == 1  # 5 // 4 = 1

    def test_longer_string(self):
        assert _estimate_tokens("a" * 100) == 25  # 100 // 4 = 25

    def test_messages_sum(self):
        msgs = [
            {"role": "user", "content": "a" * 40},
            {"role": "assistant", "content": "b" * 80},
        ]
        assert _estimate_messages_tokens(msgs) == 30  # 10 + 20


class TestTruncateToolResult:
    def test_short_result_unchanged(self):
        assert _truncate_tool_result("hello", 100) == "hello"

    def test_exact_length_unchanged(self):
        assert _truncate_tool_result("a" * 100, 100) == "a" * 100

    def test_long_result_truncated(self):
        result = _truncate_tool_result("a" * 200, 100)
        assert result.startswith("a" * 100)
        assert "truncated" in result
        assert "100 chars omitted" in result

    def test_marker_shows_correct_count(self):
        result = _truncate_tool_result("x" * 500, 100)
        assert "400 chars omitted" in result


class TestGroupIntoTurns:
    def test_empty(self):
        assert _group_into_turns([]) == []

    def test_single_user_message(self):
        msgs = [{"role": "user", "content": "hi"}]
        turns = _group_into_turns(msgs)
        assert len(turns) == 1
        assert len(turns[0]) == 1

    def test_user_assistant_pair(self):
        msgs = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ]
        turns = _group_into_turns(msgs)
        assert len(turns) == 1
        assert len(turns[0]) == 2

    def test_multiple_turns(self):
        msgs = [
            {"role": "user", "content": "1"},
            {"role": "assistant", "content": "a1"},
            {"role": "user", "content": "2"},
            {"role": "assistant", "content": "a2"},
        ]
        turns = _group_into_turns(msgs)
        assert len(turns) == 2
        assert turns[0][0]["content"] == "1"
        assert turns[1][0]["content"] == "2"


class TestFindLastCompression:
    def test_no_compression(self):
        msgs = [{"role": "user", "content": "hi"}]
        assert _find_last_compression(msgs) is None

    def test_single_compression(self):
        msgs = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "summary", "compression": True},
        ]
        assert _find_last_compression(msgs) == 1

    def test_multiple_compressions_finds_last(self):
        msgs = [
            {"role": "assistant", "content": "old summary", "compression": True},
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "new summary", "compression": True},
        ]
        assert _find_last_compression(msgs) == 2


# ---------------------------------------------------------------------------
# Bounded context builder tests
# ---------------------------------------------------------------------------


class TestBuildApiMessages:
    def test_system_prompt_always_first(self, cfg):
        sessions, manager, backend = make_manager(cfg)
        s = sessions.create("t")
        sessions.add_message(s["id"], {"id": "u1", "role": "user", "content": "hi", "ts": "t"})

        msgs = manager._build_api_messages(s["id"], SETTINGS)
        assert msgs[0]["role"] == "system"
        assert "Gremlin" in msgs[0]["content"]

    def test_window_limits_turns(self, cfg):
        """Only the last N turns are included in the API message list."""
        sessions, manager, backend = make_manager(cfg)
        s = sessions.create("t")
        # Add 5 turns (each is user + assistant)
        for i in range(5):
            _add_turn(sessions, s["id"], f"user_{i}", f"asst_{i}")

        msgs = manager._build_api_messages(s["id"], SETTINGS)
        # Window is 2 turns = 4 messages (2 user + 2 asst)
        # Plus the system prompt = 5 total
        assert msgs[0]["role"] == "system"
        # Check that only the last 2 turns are present
        contents = [m.get("content", "") for m in msgs[1:]]
        assert "user_3" in contents
        assert "asst_3" in contents
        assert "user_4" in contents
        assert "asst_4" in contents
        # Earlier turns should be dropped
        assert "user_0" not in contents
        assert "user_1" not in contents
        assert "user_2" not in contents

    def test_compression_replaces_old_history(self, cfg):
        """When a compression marker exists, pre-marker messages are replaced."""
        sessions, manager, backend = make_manager(cfg)
        s = sessions.create("t")
        sessions.add_message(s["id"], {"id": "u1", "role": "user", "content": "old question", "ts": "t"})
        sessions.add_message(s["id"], {"id": "a1", "role": "assistant", "content": "old answer", "ts": "t"})
        # Compression summary
        sessions.add_message(s["id"], {"id": "c1", "role": "assistant", "content": "SUMMARY TEXT", "compression": True, "ts": "t"})
        # Post-compression turn
        sessions.add_message(s["id"], {"id": "u2", "role": "user", "content": "new question", "ts": "t"})

        msgs = manager._build_api_messages(s["id"], SETTINGS)
        contents = [m.get("content", "") for m in msgs]
        # Old messages not re-sent
        assert "old question" not in contents
        assert "old answer" not in contents
        # Summary present as system message
        assert any("SUMMARY TEXT" in c for c in contents)
        # New question present
        assert "new question" in contents

    def test_tool_results_truncated_in_api(self, cfg):
        """Tool results exceeding max_tool_chars are truncated in API messages."""
        sessions, manager, backend = make_manager(cfg)
        s = sessions.create("t")
        large_result = "x" * 500  # exceeds tool_result_max_chars=100
        _add_turn(
            sessions, s["id"], "do thing", "ok",
            tool_calls=[{"id": "tc1", "name": "run_command", "arguments": {"cmd": "ls"}, "result": large_result, "status": "ok"}],
        )

        msgs = manager._build_api_messages(s["id"], SETTINGS)
        tool_msgs = [m for m in msgs if m.get("role") == "tool"]
        assert len(tool_msgs) == 1
        # Result is truncated
        assert len(tool_msgs[0]["content"]) < 500
        assert "truncated" in tool_msgs[0]["content"]

    def test_token_budget_drops_oldest(self, cfg):
        """When the token budget is exceeded, oldest messages are dropped."""
        sessions, manager, backend = make_manager(cfg)
        s = sessions.create("t")
        # Use a very small token budget to force truncation
        small_settings = {**SETTINGS, "max_context_tokens": 50, "context_window_turns": 10}
        # Add a large turn that will exceed the budget
        _add_turn(sessions, s["id"], "a" * 200, "b" * 200)
        _add_turn(sessions, s["id"], "recent question", "recent answer")

        msgs = manager._build_api_messages(s["id"], small_settings)
        # System prompt is always present
        assert msgs[0]["role"] == "system"
        # The recent turn should be present
        contents = [m.get("content", "") for m in msgs[1:]]
        assert "recent question" in contents


# ---------------------------------------------------------------------------
# Automatic compaction tests
# ---------------------------------------------------------------------------


class TestAutoCompaction:
    def test_triggers_when_over_threshold(self, cfg):
        """Compaction runs when stored context exceeds the threshold."""
        # Use a large response for the compaction call, then a normal one for the turn.
        backend = FakeBackend([
            [ModelEvent("text", text="COMPACTED SUMMARY"), ModelEvent("done")],
            [ModelEvent("text", text="ok"), ModelEvent("done")],
        ])
        sessions, manager, _ = make_manager(cfg, backend)
        s = sessions.create("t")
        # Add enough content to exceed the threshold (200 * 0.6 = 120 tokens = 480 chars)
        for i in range(5):
            _add_turn(sessions, s["id"], f"question {i} " + "x" * 100, f"answer {i} " + "y" * 100)

        # Run a turn — should trigger compaction first
        list(manager.run(s["id"], "more", SETTINGS))

        # The compaction summary should have been stored
        stored = sessions.get(s["id"])["messages"]
        compression_msgs = [m for m in stored if m.get("compression")]
        assert len(compression_msgs) == 1
        assert "COMPACTED SUMMARY" in compression_msgs[0]["content"]

    def test_does_not_trigger_when_under_threshold(self, cfg):
        """No compaction when context is small."""
        backend = FakeBackend([[ModelEvent("text", text="ok"), ModelEvent("done")]])
        sessions, manager, _ = make_manager(cfg, backend)
        s = sessions.create("t")
        sessions.add_message(s["id"], {"id": "u1", "role": "user", "content": "hi", "ts": "t"})

        list(manager.run(s["id"], "hello", SETTINGS))

        # No compression message should have been added
        stored = sessions.get(s["id"])["messages"]
        compression_msgs = [m for m in stored if m.get("compression")]
        assert len(compression_msgs) == 0

    def test_compaction_failure_does_not_break_turn(self, cfg):
        """If compaction fails, the turn still completes normally."""

        class FailingBackend(ModelBackend):
            def __init__(self):
                self.call_count = 0

            def stream(self, messages, tools, model):
                self.call_count += 1
                if self.call_count == 1:
                    # First call is for compaction — fail it.
                    raise ModelError("compaction boom")
                yield ModelEvent("text", text="ok")
                yield ModelEvent("done")

        backend = FailingBackend()
        sessions, manager, _ = make_manager(cfg, backend)
        s = sessions.create("t")
        for i in range(5):
            _add_turn(sessions, s["id"], f"q{i} " + "x" * 100, f"a{i} " + "y" * 100)

        events = list(manager.run(s["id"], "hello", SETTINGS))
        # Turn completed despite compaction failure
        assert events[-1]["type"] == "done"
        assert "error" not in [e["type"] for e in events]

    def test_should_compact_logic(self, cfg):
        """_should_compact returns True/False correctly."""
        sessions, manager, _ = make_manager(cfg)
        s = sessions.create("t")

        # Empty session: should not compact
        assert manager._should_compact(s["id"], SETTINGS) is False

        # Add enough to exceed threshold
        for i in range(10):
            _add_turn(sessions, s["id"], f"q{i} " + "x" * 100, f"a{i} " + "y" * 100)
        assert manager._should_compact(s["id"], SETTINGS) is True


# ---------------------------------------------------------------------------
# Tool result protection tests
# ---------------------------------------------------------------------------


class TestToolResultProtection:
    def test_large_result_truncated_in_storage(self, cfg):
        """Tool results are truncated when stored in the session."""
        backend = FakeBackend([
            [
                ModelEvent("tool_call", tool_call_id="tc1", name="read_file", arguments={"path": "big.txt"}),
                ModelEvent("done"),
            ],
            [ModelEvent("text", text="done"), ModelEvent("done")],
        ])
        # Create a big file to read
        (cfg.root / "big.txt").write_text("z" * 5000)

        sessions, manager, _ = make_manager(cfg, backend)
        s = sessions.create("t")

        events = list(manager.run(s["id"], "read big.txt", SETTINGS))
        stored = sessions.get(s["id"])["messages"][1]
        tc = stored["tool_calls"][0]
        # Stored result is truncated
        assert len(tc["result"]) < 5000
        assert "truncated" in tc["result"]
        # But the UI event has the full result
        ui_event = next(e for e in events if e["type"] == "tool_call")
        assert len(ui_event["result"]) == 5000  # full to the UI

    def test_small_result_not_truncated(self, cfg):
        """Short tool results pass through unchanged."""
        backend = FakeBackend([
            [
                ModelEvent("tool_call", tool_call_id="tc1", name="list_directory", arguments={}),
                ModelEvent("done"),
            ],
            [ModelEvent("text", text="done"), ModelEvent("done")],
        ])
        (cfg.root / "a.txt").write_text("hi")

        sessions, manager, _ = make_manager(cfg, backend)
        s = sessions.create("t")

        list(manager.run(s["id"], "list dir", SETTINGS))
        stored = sessions.get(s["id"])["messages"][1]
        tc = stored["tool_calls"][0]
        # No truncation marker
        assert "truncated" not in tc["result"]


# ---------------------------------------------------------------------------
# End-to-end: long session stays bounded
# ---------------------------------------------------------------------------


class TestLongSessionBounded:
    def test_many_turns_stay_under_limit(self, cfg):
        """After many tool-using turns, the API message list stays bounded."""
        # Backend that always returns a tool call, then text.
        responses = []
        for i in range(15):
            responses.append([
                ModelEvent("tool_call", tool_call_id=f"tc{i}", name="list_directory", arguments={}),
                ModelEvent("done"),
            ])
            responses.append([
                ModelEvent("text", text=f"answer {i}"),
                ModelEvent("done"),
            ])
        # Final turn without tool call
        responses.append([ModelEvent("text", text="final"), ModelEvent("done")])

        backend = FakeBackend(responses)
        (cfg.root / "a.txt").write_text("hi")

        sessions, manager, _ = make_manager(cfg, backend)
        s = sessions.create("t")

        # Run 15 turns
        for i in range(15):
            list(manager.run(s["id"], f"turn {i}", SETTINGS))
        # Final turn
        list(manager.run(s["id"], "final", SETTINGS))

        # Check the last API call was bounded
        last_msgs = backend.calls[-1]["messages"]
        # Should have system + (compressed or windowed) messages
        # With window=2, we should have at most ~5-6 messages
        assert len(last_msgs) < 15  # definitely bounded, not 30+ messages
        # System prompt is first
        assert last_msgs[0]["role"] == "system"
