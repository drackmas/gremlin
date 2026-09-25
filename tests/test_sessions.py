"""Session CRUD and persistence."""

from __future__ import annotations

import json
import time


def test_create_session(client, cfg):
    res = client.post("/api/sessions", json={})
    assert res.status_code == 201
    data = res.get_json()
    assert data["id"]
    assert data["title"] == "New Session"
    assert data["messages"] == []
    assert (cfg.sessions_dir / f"{data['id']}.json").exists()


def test_create_session_custom_title(client):
    res = client.post("/api/sessions", json={"title": "  My plan  "})
    assert res.get_json()["title"] == "My plan"


def test_list_sessions(client):
    a = client.post("/api/sessions", json={"title": "A"}).get_json()
    b = client.post("/api/sessions", json={"title": "B"}).get_json()
    time.sleep(0.01)
    lst = client.get("/api/sessions").get_json()
    assert {s["id"] for s in lst} == {a["id"], b["id"]}
    # newest first
    assert lst[0]["id"] == b["id"]


def test_get_session(client):
    s = client.post("/api/sessions", json={"title": "T"}).get_json()
    res = client.get(f"/api/sessions/{s['id']}")
    assert res.status_code == 200
    assert res.get_json()["id"] == s["id"]


def test_get_missing_session(client):
    res = client.get("/api/sessions/nope")
    assert res.status_code == 404
    assert "error" in res.get_json()


def test_rename_session(client, cfg):
    s = client.post("/api/sessions", json={"title": "old"}).get_json()
    res = client.patch(f"/api/sessions/{s['id']}", json={"title": "new"})
    assert res.get_json()["title"] == "new"
    on_disk = json.loads((cfg.sessions_dir / f"{s['id']}.json").read_text())
    assert on_disk["title"] == "new"


def test_delete_session(client, cfg):
    s = client.post("/api/sessions", json={}).get_json()
    res = client.delete(f"/api/sessions/{s['id']}")
    assert res.status_code == 204
    assert not (cfg.sessions_dir / f"{s['id']}.json").exists()
    assert client.get(f"/api/sessions/{s['id']}").status_code == 404


def test_invalid_session_file_skipped_in_list(client, cfg):
    bad = cfg.sessions_dir / "bad.json"
    bad.write_text("{not json")
    s = client.post("/api/sessions", json={"title": "good"}).get_json()
    lst = client.get("/api/sessions").get_json()
    assert [x["id"] for x in lst] == [s["id"]]
    # direct get on the corrupt file -> 404 error, not a crash
    res = client.get("/api/sessions/bad")
    assert res.status_code == 404


def test_add_message_bumps_updated_at(client, cfg):
    from sessions import SessionManager

    mgr = SessionManager(cfg)
    s = mgr.create("t")
    time.sleep(0.02)
    mgr.add_message(s["id"], {"id": "m1", "role": "user", "content": "hi", "ts": ""})
    loaded = mgr.get(s["id"])
    assert loaded["updated_at"] > loaded["created_at"]
    assert loaded["messages"][0]["content"] == "hi"
