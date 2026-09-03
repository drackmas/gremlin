"""GET /api/model/context: exact-id model selection, caching, graceful failure.

The endpoint must never guess: an unknown model or missing metadata degrades
to ``{"ok": false, ...}`` with HTTP 200 — never a fallback to another
model's limit, never ``n_ctx_train``.
"""

from __future__ import annotations

from unittest import mock

TWO_MODELS = [
    {"id": "m1", "object": "model", "meta": {"n_ctx": 8192, "n_ctx_train": 65536}},
    {"id": "m2", "object": "model", "meta": {"n_ctx": 131072, "n_ctx_train": 262144}},
]


def _models_response(payload):
    resp = mock.Mock(status_code=200)
    resp.json.return_value = {"data": payload}
    resp.raise_for_status.return_value = None
    return resp


def _set_model(client, model):
    res = client.post("/api/settings", json={"base_url": "http://fake/v1", "model": model})
    assert res.status_code == 200


def test_selects_exact_model_id_not_first(client):
    """m1 is listed first; the configured m2 must be matched by exact id,
    and a second request is served from the per-(base_url, model) cache."""
    _set_model(client, "m2")
    with mock.patch("app.requests.get", return_value=_models_response(TWO_MODELS)) as g:
        res = client.get("/api/model/context")
    assert res.status_code == 200
    assert res.get_json() == {"ok": True, "model": "m2", "n_ctx": 131072}
    assert g.call_count == 1

    with mock.patch("app.requests.get", return_value=_models_response(TWO_MODELS)) as g2:
        res = client.get("/api/model/context")
    assert res.get_json()["n_ctx"] == 131072
    assert g2.call_count == 0


def test_unknown_model_is_not_silently_fallback(client):
    _set_model(client, "m3")
    with mock.patch("app.requests.get", return_value=_models_response(TWO_MODELS)):
        res = client.get("/api/model/context")
    assert res.status_code == 200
    body = res.get_json()
    assert body["ok"] is False
    assert "not found" in body["error"]


def test_network_failure_returns_ok_false_http_200(client):
    _set_model(client, "m2")
    with mock.patch("app.requests.get", side_effect=Exception("boom")):
        res = client.get("/api/model/context")
    assert res.status_code == 200
    body = res.get_json()
    assert body["ok"] is False
    assert "boom" in body["error"]


def test_openai_style_max_context_length(client):
    """Without llama.cpp ``meta``, fall back to OpenAI-style fields."""
    payload = [{"id": "m1", "object": "model", "max_context_length": 4096}]
    _set_model(client, "m1")
    with mock.patch("app.requests.get", return_value=_models_response(payload)):
        res = client.get("/api/model/context")
    assert res.status_code == 200
    assert res.get_json() == {"ok": True, "model": "m1", "n_ctx": 4096}


def test_n_ctx_train_is_never_used(client):
    """n_ctx_train is the training size, not the configured capacity."""
    payload = [{"id": "m1", "object": "model", "meta": {"n_ctx_train": 262144}}]
    _set_model(client, "m1")
    with mock.patch("app.requests.get", return_value=_models_response(payload)):
        res = client.get("/api/model/context")
    assert res.status_code == 200
    assert res.get_json()["ok"] is False
