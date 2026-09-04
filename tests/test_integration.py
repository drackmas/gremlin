"""End-to-end HTTP integration: settings -> session -> chat -> verify persistence."""

from __future__ import annotations

import json
from unittest import mock


def _chat_sse(text: str = "hello from model"):
    """Build a fake streaming response: one text delta + done."""
    chunk = json.dumps(
        {"choices": [{"delta": {"role": "assistant", "content": text}, "finish_reason": None}]}
    )
    done = json.dumps(
        {"choices": [{"delta": {}, "finish_reason": "stop"}]}
    )
    lines = [
        f"data: {chunk}",
        f"data: {done}",
        "data: [DONE]",
    ]
    resp = mock.Mock()
    resp.status_code = 200
    resp.iter_lines = mock.Mock(return_value=iter(lines))
    resp.close = mock.Mock()
    return resp


def test_full_flow(client, cfg):
    """settings -> create session -> chat (SSE) -> verify persisted messages."""
    # 1. Configure settings
    res = client.post("/api/settings", json={"base_url": "http://fake/v1", "model": "m1"})
    assert res.status_code == 200
    body = res.get_json()
    assert body["base_url"] == "http://fake/v1"
    assert body["model"] == "m1"

    # 2. Create a session
    res = client.post("/api/sessions", json={"title": "integration test"})
    assert res.status_code == 201
    session = res.get_json()
    sid = session["id"]
    assert session["title"] == "integration test"

    # 3. Chat (SSE) — mock the model's streaming response
    with mock.patch("models.openai_compat.requests.Session") as MockSession:
        MockSession.return_value.post.return_value = _chat_sse("hi there")
        res = client.post(f"/api/sessions/{sid}/chat", json={"message": "hello"})
    assert res.status_code == 200
    assert res.content_type == "text/event-stream"

    # Parse SSE events
    raw = res.get_data(as_text=True)
    events = []
    for line in raw.split("\n\n"):
        line = line.strip()
        if line.startswith("data: "):
            data = line[6:]
            if data == "[DONE]":
                break
            events.append(json.loads(data))

    # Must have text event + done event
    types = [e["type"] for e in events]
    assert "text" in types
    assert "done" in types

    # 4. Verify persisted messages
    res = client.get(f"/api/sessions/{sid}")
    assert res.status_code == 200
    stored = res.get_json()
    messages = stored["messages"]
    assert len(messages) == 2
    assert messages[0]["role"] == "user"
    assert messages[0]["content"] == "hello"
    assert messages[1]["role"] == "assistant"
    assert messages[1]["content"] == "hi there"


def test_chat_requires_message(client, cfg):
    """Empty message body is rejected with 400."""
    res = client.post("/api/sessions", json={"title": "t"})
    sid = res.get_json()["id"]

    res = client.post(f"/api/sessions/{sid}/chat", json={"message": ""})
    assert res.status_code == 400
    assert "message" in res.get_json()["error"]


def test_chat_unknown_session_404(client, cfg):
    """Chat to a non-existent session returns 404."""
    res = client.post("/api/sessions/00000000deadbeef/chat", json={"message": "hi"})
    assert res.status_code == 404
