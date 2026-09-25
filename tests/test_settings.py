"""Settings persistence: defaults, save/load, validation, restart survival."""

from __future__ import annotations

import json

from chat.settings import SettingsStore


def test_default_settings(client):
    data = client.get("/api/settings").get_json()
    assert data["show_thinking"] is True
    assert data["appearance"] == "dark"
    assert data["theme"] == "default"
    assert "base_url" in data and "model" in data


def test_save_and_reload(client, cfg):
    res = client.post(
        "/api/settings",
        json={"theme": "darkly", "appearance": "light", "show_thinking": False},
    )
    assert res.status_code == 200
    saved = res.get_json()
    assert saved["theme"] == "darkly"
    assert saved["appearance"] == "light"
    assert saved["show_thinking"] is False
    # persisted to disk
    on_disk = json.loads(cfg.settings_path.read_text())
    assert on_disk["theme"] == "darkly"
    # served back on the next page load
    again = client.get("/api/settings").get_json()
    assert again["theme"] == "darkly"


def test_settings_survive_restart(client, cfg):
    client.post("/api/settings", json={"theme": "cerulean"})
    # a brand-new store instance (simulates a fresh app process)
    fresh = SettingsStore(cfg).load()
    assert fresh["theme"] == "cerulean"


def test_partial_update_keeps_other_keys(client):
    client.post("/api/settings", json={"theme": "flatly", "show_thinking": False})
    res = client.post("/api/settings", json={"appearance": "light"})
    saved = res.get_json()
    assert saved["theme"] == "flatly"
    assert saved["show_thinking"] is False
    assert saved["appearance"] == "light"


def test_invalid_appearance_rejected(client, cfg):
    before = client.get("/api/settings").get_json()
    res = client.post("/api/settings", json={"appearance": "neon"})
    assert res.status_code == 400
    assert client.get("/api/settings").get_json() == before


def test_non_bool_show_thinking_rejected(client):
    res = client.post("/api/settings", json={"show_thinking": "yes"})
    assert res.status_code == 400


def test_empty_model_rejected(client):
    res = client.post("/api/settings", json={"model": "   "})
    assert res.status_code == 400


def test_max_tool_calls_saved(client):
    res = client.post("/api/settings", json={"max_tool_calls": 42})
    assert res.status_code == 200
    assert res.get_json()["max_tool_calls"] == 42
    # reload from disk
    again = client.get("/api/settings").get_json()
    assert again["max_tool_calls"] == 42


def test_max_tool_calls_non_int_rejected(client):
    res = client.post("/api/settings", json={"max_tool_calls": "twenty"})
    assert res.status_code == 400


def test_unknown_keys_ignored(client):
    res = client.post("/api/settings", json={"theme": "darkly", "bogus": 123})
    assert res.status_code == 200
    assert "bogus" not in res.get_json()


def test_non_json_body_rejected(client):
    res = client.post("/api/settings", data="hello", content_type="text/plain")
    assert res.status_code == 400


def test_corrupt_settings_file_falls_back_to_defaults(cfg):
    cfg.settings_path.parent.mkdir(parents=True, exist_ok=True)
    cfg.settings_path.write_text("{broken")
    data = SettingsStore(cfg).load()
    expected = dict(cfg.DEFAULT_SETTINGS)
    expected["discord_token_set"] = False  # derived flag; no .env in the tmp tree
    assert data == expected


def test_discord_token_saved_to_env(cfg):
    store = SettingsStore(cfg)
    assert store.load()["discord_token_set"] is False
    # no token yet -> no .env file created
    store.save({"discord_enabled": True})
    assert not cfg.env_path.exists()
    # providing a token writes it to .env, never into settings.json
    store.save({"GREMLIN_DISCORD_TOKEN": "  tok123  "})
    assert cfg.env_path.exists()
    assert "tok123" in cfg.env_path.read_text(encoding="utf-8")
    assert "GREMLIN_DISCORD_TOKEN" not in json.loads(cfg.settings_path.read_text())
    assert store.load()["discord_token_set"] is True
    # a blank token must not clobber an existing one
    store.save({"GREMLIN_DISCORD_TOKEN": "   "})
    assert store.load()["discord_token_set"] is True


def test_themes_endpoint(client, cfg):
    # no themes bundled in the tmp tree -> just the default
    themes = client.get("/api/themes").get_json()
    assert themes == ["default"]
    # add a couple of fake theme files
    cfg.themes_dir.mkdir(parents=True, exist_ok=True)
    (cfg.themes_dir / "darkly.min.css").write_text("/* t */")
    (cfg.themes_dir / "cerulean.min.css").write_text("/* t */")
    themes = client.get("/api/themes").get_json()
    assert themes == ["default", "cerulean", "darkly"]


def test_compaction_threshold_float_saved(client, cfg):
    res = client.post("/api/settings", json={"compaction_threshold": 0.8})
    assert res.status_code == 200
    assert res.get_json()["compaction_threshold"] == 0.8
    # persisted to disk, not just echoed
    on_disk = json.loads(cfg.settings_path.read_text())
    assert on_disk["compaction_threshold"] == 0.8


def test_compaction_threshold_boundaries_accepted(client):
    for ok in (0.05, 0.5, 1):
        res = client.post("/api/settings", json={"compaction_threshold": ok})
        assert res.status_code == 200, ok


def test_compaction_threshold_out_of_range_rejected(client):
    for bad in (0, 0.0, 1.5, -0.2, "0.5", None):
        res = client.post("/api/settings", json={"compaction_threshold": bad})
        assert res.status_code == 400, bad


def test_context_ints_must_be_positive(client):
    for key in ("max_context_tokens", "context_window_turns", "tool_result_max_chars"):
        res = client.post("/api/settings", json={key: 0})
        assert res.status_code == 400, key
        res = client.post("/api/settings", json={key: -5})
        assert res.status_code == 400, key


def test_max_tool_calls_zero_rejected(client):
    res = client.post("/api/settings", json={"max_tool_calls": 0})
    assert res.status_code == 400


def test_context_int_rejects_bool_and_float(client):
    res = client.post("/api/settings", json={"max_context_tokens": True})
    assert res.status_code == 400
    res = client.post("/api/settings", json={"max_context_tokens": 32768.5})
    assert res.status_code == 400
