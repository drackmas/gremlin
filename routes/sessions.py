"""Session CRUD routes: list, create, get, rename, delete, compress."""

from __future__ import annotations

from flask import Blueprint, jsonify, request

from chat.manager import ChatManager
from models.base import ModelError
from sessions import SessionError, SessionManager


def create_bp(sessions: SessionManager, manager: ChatManager, settings_loader) -> Blueprint:
    """Build and return the sessions Blueprint."""
    bp = Blueprint("sessions", __name__)

    @bp.get("/api/sessions")
    def list_sessions():
        return jsonify(sessions.list())

    @bp.post("/api/sessions")
    def create_session():
        data = request.get_json(silent=True) or {}
        return jsonify(sessions.create(data.get("title", "New Session"))), 201

    @bp.get("/api/sessions/<session_id>")
    def get_session(session_id):
        try:
            return jsonify(sessions.get(session_id))
        except SessionError as e:
            return jsonify({"error": str(e)}), 404

    @bp.patch("/api/sessions/<session_id>")
    def rename_session(session_id):
        data = request.get_json(silent=True) or {}
        try:
            return jsonify(sessions.rename(session_id, data.get("title", "")))
        except SessionError as e:
            return jsonify({"error": str(e)}), 404

    @bp.delete("/api/sessions/<session_id>")
    def delete_session(session_id):
        try:
            sessions.delete(session_id)
        except SessionError as e:
            return jsonify({"error": str(e)}), 404
        return "", 204

    @bp.post("/api/sessions/<session_id>/compress")
    def compress_session(session_id):
        try:
            sessions.get(session_id)
        except SessionError as e:
            return jsonify({"error": str(e)}), 404
        try:
            msg = manager.compress(session_id, settings_loader())
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        except ModelError as e:
            return jsonify({"error": str(e)}), 502
        return jsonify({"ok": True, "chars": len(msg["content"])})

    return bp
