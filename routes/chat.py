"""Chat routes: send (SSE stream) and abort."""

from __future__ import annotations

import json
import logging

from flask import Blueprint, Response, jsonify, request

from chat.manager import ChatManager
from sessions import SessionError, SessionManager

log = logging.getLogger("gremlin")


def create_bp(manager: ChatManager, sessions: SessionManager, settings_loader) -> Blueprint:
    """Build and return the chat Blueprint."""
    bp = Blueprint("chat", __name__)

    @bp.post("/api/sessions/<session_id>/chat")
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
                for payload in manager.run(session_id, message, settings_loader()):
                    yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
            except Exception as e:  # never let the stream die silently
                log.exception("chat stream failed")
                yield f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n"
                yield f"data: {json.dumps({'type': 'done'})}\n\n"

        resp = Response(event_stream(), content_type="text/event-stream")
        resp.headers["Cache-Control"] = "no-cache"
        resp.headers["X-Accel-Buffering"] = "no"
        return resp

    @bp.post("/api/sessions/<session_id>/abort")
    def abort_session(session_id):
        """Cooperative Stop: cancel the session's active generation, if any."""
        try:
            sessions.get(session_id)
        except SessionError as e:
            return jsonify({"error": str(e)}), 404
        cancelled = manager.abort(session_id)
        return jsonify({"ok": True, "cancelled": cancelled})

    return bp
