"""Gremlin — local, self-contained chat assistant.

Flask front door: serves the single-page UI and a small JSON/SSE API.
All persistence and model logic lives in the packages below.
"""

from __future__ import annotations

import json
import logging
import os
import sys

from dotenv import load_dotenv
from flask import Flask, Response, jsonify, render_template, request
import requests

from chat.manager import ChatManager
from models.base import ModelError
from chat.settings import SettingsError, SettingsStore
from memory.store import MemoryStore

from config import AppConfig, ensure_dirs
from sessions import SessionError, SessionManager
from skills.loader import SkillLoader
from tools import build_registry

log = logging.getLogger("gremlin")


def _setup_logging(cfg: AppConfig) -> None:
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for h in list(root.handlers):
        root.removeHandler(h)
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    root.addHandler(console)
    fileh = logging.FileHandler(cfg.data_dir / "gremlin.log", encoding="utf-8")
    fileh.setFormatter(fmt)
    root.addHandler(fileh)
    logging.getLogger("werkzeug").setLevel(logging.WARNING)


def _model_context(base_url: str, model: str, cache: dict[tuple[str, str], int]) -> dict:
    """Effective context window for the configured model, from the server.

    Cached per (base_url, model); changing either is a natural cache miss.
    Never guesses: an unknown model or missing metadata degrades to
    ``{"ok": false, ...}`` — no silent fallback to another model's limit.
    """
    key = (base_url, model)
    cached = cache.get(key)
    if cached is not None:
        return {"ok": True, "model": model, "n_ctx": cached}
    if not base_url or not model:
        return {"ok": False, "error": "base_url or model not configured"}
    url = base_url if base_url.endswith("/models") else base_url + "/models"
    try:
        resp = requests.get(url, timeout=3)
        resp.raise_for_status()
        data = resp.json().get("data") or []
    except Exception as e:
        log.warning("model context query failed: %s", e)
        return {"ok": False, "error": f"cannot query models endpoint: {e}"}
    entry = next((m for m in data if isinstance(m, dict) and m.get("id") == model), None)
    if entry is None:
        return {"ok": False, "error": f"model '{model}' not found"}
    meta = entry.get("meta") or {}
    n_ctx = meta.get("n_ctx")
    if not isinstance(n_ctx, int) or n_ctx <= 0:
        n_ctx = entry.get("max_context_length")
    if not isinstance(n_ctx, int) or n_ctx <= 0:
        n_ctx = entry.get("context_length")
    if not isinstance(n_ctx, int) or n_ctx <= 0:
        return {"ok": False, "error": f"no context length reported for model '{model}'"}
    cache[key] = n_ctx
    return {"ok": True, "model": model, "n_ctx": n_ctx}


def create_app(cfg: AppConfig | None = None) -> Flask:
    cfg = cfg or AppConfig()
    ensure_dirs(cfg)
    _setup_logging(cfg)
    load_dotenv(cfg.env_path)  # expose GREMLIN_DISCORD_TOKEN (and friends) from .env

    app = Flask(
        __name__,
        template_folder=str(cfg.templates_dir),
        static_folder=str(cfg.static_dir),
        static_url_path="/static",
    )
    app.config["JSON_SORT_KEYS"] = False
    sessions = SessionManager(cfg)
    settings = SettingsStore(cfg)
    skills = SkillLoader(cfg)
    memory = MemoryStore(cfg.data_dir / "memory.json")
    registry = build_registry(cfg, skills, memory)
    manager = ChatManager(cfg, sessions, registry, skills, memory=memory)

    # --- discord bot (opt-in; start/stop tracked by the settings toggle) --
    from discord_bot import DiscordBot

    discord_bot = DiscordBot(
        manager,
        sessions,
        settings.load,
        cfg.data_dir / "discord_sessions.json",
    )
    app.extensions["gremlin_discord"] = discord_bot
    if settings.load().get("discord_enabled"):
        try:
            discord_bot.start()
            log.info("discord bot started at boot")
        except ValueError as e:
            log.warning("discord bot not started at boot: %s", e)

    # --- pages ----------------------------------------------------------
    @app.get("/")
    def index():
        return render_template("index.html")

    # --- sessions ---------------------------------------------------------
    @app.get("/api/sessions")
    def list_sessions():
        return jsonify(sessions.list())

    @app.post("/api/sessions")
    def create_session():
        data = request.get_json(silent=True) or {}
        return jsonify(sessions.create(data.get("title", "New Session"))), 201

    @app.get("/api/sessions/<session_id>")
    def get_session(session_id):
        try:
            return jsonify(sessions.get(session_id))
        except SessionError as e:
            return jsonify({"error": str(e)}), 404

    @app.patch("/api/sessions/<session_id>")
    def rename_session(session_id):
        data = request.get_json(silent=True) or {}
        try:
            return jsonify(sessions.rename(session_id, data.get("title", "")))
        except SessionError as e:
            return jsonify({"error": str(e)}), 404

    @app.delete("/api/sessions/<session_id>")
    def delete_session(session_id):
        try:
            sessions.delete(session_id)
        except SessionError as e:
            return jsonify({"error": str(e)}), 404
        return "", 204

    @app.post("/api/sessions/<session_id>/compress")
    def compress_session(session_id):
        try:
            sessions.get(session_id)
        except SessionError as e:
            return jsonify({"error": str(e)}), 404
        try:
            msg = manager.compress(session_id, settings.load())
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        except ModelError as e:
            return jsonify({"error": str(e)}), 502
        return jsonify({"ok": True, "chars": len(msg["content"])})

    # --- chat (SSE) -------------------------------------------------------
    @app.post("/api/sessions/<session_id>/chat")
    def chat(session_id):
        data = request.get_json(silent=True) or {}
        message = (data.get("message") or "").strip()
        if not message:
            return jsonify({"error": "message is required"}), 400
        try:
            sessions.get(session_id)
        except SessionError as e:
            return jsonify({"error": str(e)}), 404

        def event_stream():
            try:
                for payload in manager.run(session_id, message, settings.load()):
                    yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
            except Exception as e:  # never let the stream die silently
                log.exception("chat stream failed")
                yield f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n"
                yield f"data: {json.dumps({'type': 'done'})}\n\n"

        resp = Response(event_stream(), content_type="text/event-stream")
        resp.headers["Cache-Control"] = "no-cache"
        resp.headers["X-Accel-Buffering"] = "no"
        return resp

    # --- settings ---------------------------------------------------------
    @app.get("/api/settings")
    def get_settings():
        return jsonify(settings.load())

    @app.post("/api/settings")
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
    # --- model -------------------------------------------------------------
    model_context_cache: dict[tuple[str, str], int] = {}

    @app.get("/api/model/context")
    def model_context():
        s = settings.load()
        base_url = (s.get("base_url") or "").rstrip("/")
        model = (s.get("model") or "").strip()
        return jsonify(_model_context(base_url, model, model_context_cache))

    # --- themes -----------------------------------------------------------
    @app.get("/api/themes")
    def themes():
        out = ["default"]
        if cfg.themes_dir.is_dir():
            for p in sorted(cfg.themes_dir.glob("*.css")):
                slug = p.name[:-8] if p.name.endswith(".min.css") else p.stem
                out.append(slug)
        return jsonify(out)

    log.info("gremlin app ready (root=%s, sessions=%d)", cfg.root, len(sessions.list()))
    # --- API bridge (opt-in) ---------------------------------------------
    if cfg.bridge_enabled or os.environ.get("GREMLIN_BRIDGE") == "1":
        from bridge import BridgeServer

        bridge = BridgeServer(
            manager,
            sessions,
            settings.load,
            host=cfg.bridge_host,
            port=cfg.bridge_port,
            api_key=os.environ.get("GREMLIN_BRIDGE_KEY") or cfg.bridge_key,
        )
        try:
            bridge.start()
            log.info("API bridge listening on %s:%d", cfg.bridge_host, bridge.bound_port)
            app.extensions["gremlin_bridge"] = bridge
        except (OSError, ValueError) as e:
            log.error("API bridge failed to start: %s", e)
            app.extensions["gremlin_bridge"] = None

    return app


def main() -> None:
    cfg = AppConfig()
    app = create_app(cfg)
    host = os.environ.get("GREMLIN_HOST", "127.0.0.1")
    port = int(os.environ.get("GREMLIN_PORT", "7860"))
    log.info("starting gremlin on http://%s:%d", host, port)
    app.run(host=host, port=port, debug=True, threaded=True)


if __name__ == "__main__":
    main()
