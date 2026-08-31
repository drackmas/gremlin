"""Gremlin — local, self-contained chat assistant.

Flask front door: serves the single-page UI and a small JSON/SSE API.
All persistence and model logic lives in the packages below.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path

from flask import Flask, Response, jsonify, render_template, request

from chat.manager import ChatManager
from chat.settings import SettingsError, SettingsStore
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


def create_app(cfg: AppConfig | None = None) -> Flask:
    cfg = cfg or AppConfig()
    ensure_dirs(cfg)
    _setup_logging(cfg)

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
    registry = build_registry(cfg, skills)
    manager = ChatManager(cfg, sessions, registry, skills)

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
            return jsonify(settings.save(data))
        except SettingsError as e:
            return jsonify({"error": str(e)}), 400

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
    return app


def main() -> None:
    cfg = AppConfig()
    app = create_app(cfg)
    host = os.environ.get("GREMLIN_HOST", "127.0.0.1")
    port = int(os.environ.get("GREMLIN_PORT", "7860"))
    log.info("starting gremlin on http://%s:%d", host, port)
    app.run(host=host, port=port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
