"""API bridge: raw-socket OpenAI-compatible server (live socket tests)."""

from __future__ import annotations

import http.client
import json

import pytest

from bridge import BridgeServer
from chat.manager import ChatManager
from models.base import ModelBackend, ModelError, ModelEvent
from sessions import SessionManager
from skills.loader import SkillLoader
from tools import build_registry
import re

KEY = "test-key"
SETTINGS = {"show_thinking": True, "base_url": "http://fake", "model": "fake-model"}


class FakeBackend(ModelBackend):
    """Scripted backend: each call pops the next list of events."""

    def __init__(self, scripts):
        self.scripts = scripts
        self.calls = []

    def stream(self, messages, tools, model):
        self.calls.append({"messages": json.loads(json.dumps(messages)), "tools": tools, "model": model})
        for ev in self.scripts.pop(0):
            yield ev


class BoomBackend(ModelBackend):
    def stream(self, messages, tools, model):
        raise ModelError("boom")
        yield  # pragma: no cover


@pytest.fixture
def bridge(cfg):
    backend = FakeBackend([])
    sessions = SessionManager(cfg)
    manager = ChatManager(cfg, sessions, build_registry(cfg), SkillLoader(cfg), backend)
    srv = BridgeServer(manager, sessions, lambda: dict(SETTINGS), port=0, api_key=KEY)
    srv.start()
    yield srv, manager, sessions, backend
    srv.stop()


def _req(port, method, path, body=None, key=KEY, extra=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    headers = {"Content-Type": "application/json"}
    if key is not None:
        headers["Authorization"] = f"Bearer {key}"
    headers.update(extra or {})
    payload = json.dumps(body) if body is not None else None
    conn.request(method, path, body=payload, headers=headers)
    resp = conn.getresponse()
    data = resp.read()
    conn.close()
    return resp, data


def test_requires_key(cfg):
    with pytest.raises(ValueError):
        BridgeServer(None, None, None, api_key="")


def test_health(bridge):
    srv, _m, _s, _b = bridge
    resp, data = _req(srv.bound_port, "GET", "/health")
    assert resp.status == 200
    assert json.loads(data) == {"ok": True}


def test_auth_rejected(bridge):
    srv, _m, _s, _b = bridge
    resp, data = _req(srv.bound_port, "GET", "/health", key=None)
    assert resp.status == 401
    assert "bearer" in json.loads(data)["error"]["message"]


def test_models(bridge):
    srv, _m, _s, _b = bridge
    resp, data = _req(srv.bound_port, "GET", "/v1/models")
    assert resp.status == 200
    assert json.loads(data)["data"][0]["id"] == "fake-model"


def test_completion_non_stream(bridge):
    srv, manager, sessions, backend = bridge
    backend.scripts = [[
        ModelEvent(kind="text", text="Hello "),
        ModelEvent(kind="text", text="there"),
        ModelEvent(kind="done"),
    ]]
    resp, data = _req(
        srv.bound_port,
        "POST",
        "/v1/chat/completions",
        body={"model": "fake-model", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status == 200
    out = json.loads(data)
    assert out["object"] == "chat.completion"
    assert out["choices"][0]["message"]["content"] == "Hello there"
    assert out["choices"][0]["finish_reason"] == "stop"
    sid = resp.getheader("X-Gremlin-Session")
    assert sid and sessions.get(sid)["messages"][-1]["role"] == "assistant"


def test_completion_stream(bridge):
    srv, manager, sessions, backend = bridge
    backend.scripts = [[
        ModelEvent(kind="text", text="Hello "),
        ModelEvent(kind="text", text="there"),
        ModelEvent(kind="done"),
    ]]
    resp, data = _req(
        srv.bound_port,
        "POST",
        "/v1/chat/completions",
        body={"model": "fake-model", "stream": True, "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status == 200
    assert b"chat.completion.chunk" in data
    assert b'"content": "Hello "' in data
    assert data.endswith(b"data: [DONE]\n\n")


def test_completion_error_is_502(bridge):
    srv, manager, sessions, backend = bridge
    manager._backend = BoomBackend()
    resp, data = _req(
        srv.bound_port,
        "POST",
        "/v1/chat/completions",
        body={"messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status == 502
    assert json.loads(data)["error"]["message"] == "boom"


def test_stream_error_in_band(bridge):
    srv, manager, sessions, backend = bridge
    manager._backend = BoomBackend()
    resp, data = _req(
        srv.bound_port,
        "POST",
        "/v1/chat/completions",
        body={"stream": True, "messages": [{"role": "user", "content": "hi"}]},
    )
    # Head already sent: 200, error signaled inside the stream, clean end.
    assert resp.status == 200
    assert b"(error: boom)" in data
    assert data.endswith(b"data: [DONE]\n\n")


def test_session_resume(bridge):
    srv, manager, sessions, backend = bridge
    backend.scripts = [
        [ModelEvent(kind="text", text="ok"), ModelEvent(kind="done")],
        [ModelEvent(kind="text", text="ok"), ModelEvent(kind="done")],
    ]
    resp1, _ = _req(
        srv.bound_port,
        "POST",
        "/v1/chat/completions",
        body={"messages": [{"role": "user", "content": "first"}]},
    )
    sid = resp1.getheader("X-Gremlin-Session")
    resp2, _ = _req(
        srv.bound_port,
        "POST",
        "/v1/chat/completions",
        body={"messages": [{"role": "user", "content": "second"}]},
        extra={"X-Gremlin-Session": sid},
    )
    assert resp2.getheader("X-Gremlin-Session") == sid
    # Second model call sees the full conversation: system, u1, a1, u2.
    assert len(backend.calls[1]["messages"]) == 4


def test_seeded_history(bridge):
    srv, manager, sessions, backend = bridge
    backend.scripts = [[ModelEvent(kind="text", text="ok"), ModelEvent(kind="done")]]
    _resp, _ = _req(
        srv.bound_port,
        "POST",
        "/v1/chat/completions",
        body={
            "messages": [
                {"role": "user", "content": "earlier"},
                {"role": "assistant", "content": "earlier reply"},
                {"role": "user", "content": "now"},
            ]
        },
    )
    # system + seeded (u, a) + new user
    assert len(backend.calls[0]["messages"]) == 4


def test_bad_last_message(bridge):
    srv, _m, _s, _b = bridge
    resp, data = _req(
        srv.bound_port,
        "POST",
        "/v1/chat/completions",
        body={"messages": [{"role": "assistant", "content": "hi"}]},
    )
    assert resp.status == 400
    assert "last message" in json.loads(data)["error"]["message"]


def test_unknown_session(bridge):
    srv, _m, _s, _b = bridge
    resp, data = _req(
        srv.bound_port,
        "POST",
        "/v1/chat/completions",
        body={"messages": [{"role": "user", "content": "hi"}]},
        extra={"X-Gremlin-Session": "nope"},
    )
    assert resp.status == 404

def test_seed_timestamp_is_utc(bridge):
    srv, manager, sessions, backend = bridge
    backend.scripts = [[ModelEvent(kind="text", text="ok"), ModelEvent(kind="done")]]
    resp, _ = _req(
        srv.bound_port,
        "POST",
        "/v1/chat/completions",
        body={
            "messages": [
                {"role": "user", "content": "earlier"},
                {"role": "user", "content": "now"},
            ]
        },
    )
    assert resp.status == 200
    sid = resp.getheader("X-Gremlin-Session")
    seeded = sessions.get(sid)["messages"][0]  # the seeded "earlier" turn
    # now_utc() yields ISO-8601 with a UTC offset (…+00:00); local time would not.
    assert re.search(r"Z$|\+00:00$", seeded["ts"]), seeded["ts"]
