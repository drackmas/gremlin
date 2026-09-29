"""Settings routes: get and save."""

from __future__ import annotations

import logging

from flask import Blueprint, jsonify, request

from chat.settings import SettingsError, SettingsStore

log = logging.getLogger("gremlin")


def create_bp(settings: SettingsStore, discord_bot) -> Blueprint:
    """Build and return the settings Blueprint."""
    bp = Blueprint("settings", __name__)

    @bp.get("/api/settings")
    def get_settings():
        return jsonify(settings.load())

    @bp.post("/api/settings")
    def save_settings():
        data = request.get_json(silent=True)
        if data is None:
            return jsonify({"error": "JSON body required"}), 400
        try:
            before = settings.load()
            saved = settings.save(data)
        except SettingsError as e:
            return jsonify({"error": str(e)}), 400
        was, now = before.get("discord_enabled"), saved.get("discord_enabled")
        if now and not was:
            try:
                discord_bot.start()
                log.info("discord bot started from settings toggle")
            except ValueError as e:
                log.warning("discord start failed: %s", e)
        elif was and not now:
            discord_bot.stop()
            log.info("discord bot stopped from settings toggle")
        return jsonify(saved)

    return bp
