"""OpenAI-compatible backend: connection reuse (keep-alive)."""

from __future__ import annotations

import json
import requests
from unittest import mock

from models.base import ModelError
from models.openai_compat import OpenAICompatBackend


def _sse_response():
    """Minimal fake streaming response: one text delta, then [DONE]."""
    lines = iter(['data: {"choices":[{"delta":{"content":"hi"}}]}', "data: [DONE]"])
    resp = mock.Mock(status_code=200, text="")
    resp.iter_lines.return_value = lines
    return resp


def test_backend_reuses_one_session():
    """A single backend instance must POST through one persistent Session so
    consecutive turns reuse the connection instead of re-handshaking (#8)."""
    backend = OpenAICompatBackend("http://fake/v1")
    assert isinstance(backend._session, requests.Session)

    with mock.patch.object(backend._session, "post", return_value=_sse_response()) as post:
        # Two turns through the same backend instance (generator must be consumed).
        for _ in range(2):
            list(backend.stream([], [], "fake"))

    # Both calls went through the same session object, exactly once each.
    assert post.call_count == 2


def test_backend_rejects_empty_base_url():
    try:
        OpenAICompatBackend("")
    except ModelError:
        pass
    else:
        raise AssertionError("expected ModelError for empty base_url")


def _sse_line(obj):
    """Wrap a parsed SSE payload object as a raw ``data: ...`` line."""
    return "data: " + json.dumps(obj)


def _sse_stream(lines):
    """Build a fake streaming response from a list of raw ``data:`` lines."""
    resp = mock.Mock(status_code=200, text="")
    resp.iter_lines.return_value = iter(lines)
    return resp


def test_backend_reassembles_split_tool_call():
    """A tool call split across >=3 chunks (id, name, arguments each split) must
    be reassembled into a single tool_call event before `done` (Phase 1a)."""
    backend = OpenAICompatBackend("http://fake/v1")
    resp = _sse_stream(
        [
            _sse_line({"choices": [{"delta": {"tool_calls": [
                {"index": 0, "id": "call_abc", "function": {"name": "re", "arguments": "{\"path\":"}}]}}]}),
            _sse_line({"choices": [{"delta": {"tool_calls": [
                {"index": 0, "function": {"name": "ad_file", "arguments": "\"x\"}"}}]}}]}),
            _sse_line({"choices": [{"delta": {}, "finish_reason": "tool_calls"}]}),
        ]
    )
    with mock.patch.object(backend._session, "post", return_value=resp):
        events = list(backend.stream([], [], "fake"))

    assert [e.kind for e in events] == ["tool_call", "done"]
    tc = events[0]
    assert tc.tool_call_id == "call_abc"
    assert tc.name == "read_file"
    assert tc.arguments == {"path": "x"}
    assert events[1].kind == "done"
    assert events[1].finish_reason == "tool_calls"


def test_backend_defaults_finish_reason_when_stream_has_none():
    """A stream ending without [DONE] and without a finish_reason must still yield a
    terminal `done` with finish_reason 'stop' (Phase 1a)."""
    backend = OpenAICompatBackend("http://fake/v1")
    resp = _sse_stream(
        [
            _sse_line({"choices": [{"delta": {"content": "hi"}}]}),
        ]
    )
    with mock.patch.object(backend._session, "post", return_value=resp):
        events = list(backend.stream([], [], "fake"))

    assert [e.kind for e in events] == ["text", "done"]
    assert events[1].kind == "done"
    assert events[1].finish_reason == "stop"
def test_backend_captures_stream_usage_and_requests_it():
    """The payload must request usage (stream_options) and the terminal `done`
    event must carry the provider usage from the empty-choices chunk that
    llama.cpp sends AFTER the finish_reason chunk (real wire order:
    text deltas -> finish_reason chunk -> usage chunk -> [DONE])."""
    usage = {"prompt_tokens": 42, "completion_tokens": 7, "total_tokens": 49}
    backend = OpenAICompatBackend("http://fake/v1")
    resp = _sse_stream(
        [
            _sse_line({"choices": [{"delta": {"content": "hi"}}]}),
            _sse_line({"choices": [{"delta": {}, "finish_reason": "stop"}]}),
            _sse_line({"choices": [], "usage": usage}),
            "data: [DONE]",
        ]
    )
    with mock.patch.object(backend._session, "post", return_value=resp) as post:
        events = list(backend.stream([], [], "fake"))

    payload = post.call_args.kwargs["json"]
    assert payload["stream_options"] == {"include_usage": True}
    done = [e for e in events if e.kind == "done"]
    assert len(done) == 1
    assert done[0].finish_reason == "stop"
    assert done[0].usage == usage


def test_backend_carries_usage_on_fallback_done():
    """A stream that ends without [DONE] must still carry the captured usage on
    the fallback terminal `done` — the buffered finish reason survives, and the
    trailing usage chunk (after the finish chunk) is captured before EOF."""
    usage = {"prompt_tokens": 5, "completion_tokens": 1, "total_tokens": 6}
    backend = OpenAICompatBackend("http://fake/v1")
    resp = _sse_stream(
        [
            _sse_line({"choices": [{"delta": {"content": "x"}}]}),
            _sse_line({"choices": [{"delta": {}, "finish_reason": "tool_calls"}]}),
            _sse_line({"choices": [], "usage": usage}),
        ]
    )
    with mock.patch.object(backend._session, "post", return_value=resp):
        events = list(backend.stream([], [], "fake"))
    done = [e for e in events if e.kind == "done"]
    assert len(done) == 1
    assert done[0].finish_reason == "tool_calls"
    assert done[0].usage == usage
