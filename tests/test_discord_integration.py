"""Integration: the settings toggle starts/stops the Discord bot.

The real ``DiscordBot.start`` is used where it is safe (no token -> raises
before importing discord). Where a live connection would be attempted, the
class methods are monkeypatched to record calls instead.
"""

from __future__ import annotations

import discord_bot as db_mod


def test_settings_expose_discord_keys(client):
    data = client.get("/api/settings").get_json()
    assert data["discord_enabled"] is False
    assert data["discord_token_set"] is False


def test_enable_without_token_does_not_start(client, app, monkeypatch):
    monkeypatch.delenv("GREMLIN_DISCORD_TOKEN", raising=False)
    res = client.post("/api/settings", json={"discord_enabled": True})
    assert res.status_code == 200
    assert res.get_json()["discord_enabled"] is True
    # no token -> start() raised and was swallowed; bot is not running
    assert app.extensions["gremlin_discord"].running is False


def test_token_saved_sets_flag(client, cfg):
    res = client.post("/api/settings", json={"GREMLIN_DISCORD_TOKEN": "  abc123  "})
    assert res.status_code == 200
    assert cfg.env_path.exists()
    assert "abc123" in cfg.env_path.read_text(encoding="utf-8")
    again = client.get("/api/settings").get_json()
    assert again["discord_token_set"] is True
    # the raw token is never echoed back to the client
    assert "GREMLIN_DISCORD_TOKEN" not in again


def test_toggle_starts_and_stops_bot(client, monkeypatch):
    calls = []
    monkeypatch.setattr(db_mod.DiscordBot, "start", lambda self: calls.append("start"))
    monkeypatch.setattr(db_mod.DiscordBot, "stop", lambda self: calls.append("stop"))
    client.post("/api/settings", json={"discord_enabled": True})
    assert calls == ["start"]
    client.post("/api/settings", json={"discord_enabled": False})
    assert calls == ["start", "stop"]
    # toggling on twice is idempotent at the route level (no second start)
    client.post("/api/settings", json={"discord_enabled": True})
    client.post("/api/settings", json={"discord_enabled": True})
    assert calls.count("start") == 2  # off->on counts once per transition


def test_boot_starts_bot_when_enabled(cfg, monkeypatch):
    from app import create_app
    from chat.settings import SettingsStore

    store = SettingsStore(cfg)
    store.save({"discord_enabled": True, "GREMLIN_DISCORD_TOKEN": "boot-tok"})
    started = []
    monkeypatch.setattr(db_mod.DiscordBot, "start", lambda self: started.append(1))
    create_app(cfg)
    assert started == [1], "bot should start at boot when enabled and a token is set"


def test_boot_stays_off_when_disabled(cfg, monkeypatch):
    from app import create_app

    started = []
    monkeypatch.setattr(db_mod.DiscordBot, "start", lambda self: started.append(1))
    create_app(cfg)
    assert started == [], "bot must not start at boot when the toggle is off"
