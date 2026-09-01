"""OpenAI-compatible API bridge.

A raw-socket HTTP server (no HTTP framework) that exposes Gremlin's
ChatManager as a model endpoint, so external agents, the OpenAI SDK, or
plain curl can use it:

    GET  /health                 -> {"ok": true}
    GET  /v1/models              -> {"object": "list", "data": [...]}
    POST /v1/chat/completions    -> completion (JSON, or SSE when stream=true)

Auth: ``Authorization: Bearer <key>`` -- the key is mandatory. Binds to
127.0.0.1 by default. Session continuity across calls is optional via the
``X-Gremlin-Session`` header (echoed on every response).

One thread per connection; responses are ``Connection: close``. Once the
response head has been sent (e.g. an SSE stream started), errors can no
longer be reported as HTTP status lines -- they are signaled in-band and
the stream is closed.
"""

from __future__ import annotations

import json
import logging
import socket
import threading
import time
import uuid
from datetime import datetime

log = logging.getLogger("gremlin.bridge")

MAX_HEAD = 65536
MAX_BODY = 4 * 1024 * 1024  # 4 MB request cap


class BridgeError(Exception):
    """HTTP-level failure with a status code."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def _now() -> str:
    return datetime.now().astimezone().isoformat()


class BridgeServer:
    def __init__(self, manager, sessions, settings_fn, host="127.0.0.1", port=8787, api_key="") -> None:
        if not api_key:
            raise ValueError("bridge requires an api_key")
        self.manager = manager
        self.sessions = sessions
        self.settings_fn = settings_fn
        self.host = host
        self.port = port
        self.api_key = api_key
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stopping = False

    # -- lifecycle ------------------------------------------------------
    def start(self) -> int:
        """Bind and start accepting. Returns the actual bound port."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        # A short accept timeout keeps stop() responsive: closing a socket
        # does not reliably wake a thread blocked in accept() on Linux.
        sock.settimeout(0.5)
        sock.bind((self.host, self.port))
        sock.listen(16)
        self._sock = sock
        self._stopping = False
        self._thread = threading.Thread(target=self._accept_loop, name="gremlin-bridge", daemon=True)
        self._thread.start()
        log.info("bridge listening on %s:%d", self.host, self.bound_port)
        return self.bound_port

    def stop(self) -> None:
        self._stopping = True
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass
        if self._thread:
            self._thread.join(timeout=2)

    @property
    def bound_port(self) -> int:
        if self._sock:
            return self._sock.getsockname()[1]
        return self.port

    def _accept_loop(self) -> None:
        while not self._stopping:
            try:
                conn, _addr = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._handle_conn, args=(conn,), daemon=True).start()

    # -- connection handling --------------------------------------------
    def _handle_conn(self, conn: socket.socket) -> None:
        conn.settimeout(30)
        # Per-connection state: once the response head is out, HTTP status
        # errors are impossible -- later failures must go in-band or log.
        state = {"headers_sent": False}
        try:
            (method, path, headers), body = self._read_request(conn)
            self._route(conn, method, path, headers, body, state)
        except BridgeError as e:
            self._send_error(conn, e.status, e.message, state)
        except (ConnectionError, socket.timeout, ValueError):
            pass
        except Exception:
            log.exception("bridge connection error")
            self._send_error(conn, 500, "internal bridge error", state)
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def _read_request(self, conn: socket.socket) -> tuple[tuple[str, str, dict], bytes]:
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = conn.recv(MAX_HEAD)
            if not chunk:
                raise ValueError("connection closed")
            buf += chunk
            if len(buf) > MAX_HEAD:
                raise BridgeError(431, "request head too large")
        head, _, rest = buf.partition(b"\r\n\r\n")
        lines = head.decode("latin-1").split("\r\n")
        parts = lines[0].split(" ", 2)
        if len(parts) != 3:
            raise ValueError("bad request line")
        method, path, _ver = parts
        headers: dict[str, str] = {}
        for line in lines[1:]:
            if ":" in line:
                k, v = line.split(":", 1)
                headers[k.strip().lower()] = v.strip()
        length = int(headers.get("content-length") or 0)
        if length > MAX_BODY:
            raise BridgeError(413, "request body too large")
        body = rest
        while len(body) < length:
            chunk = conn.recv(MAX_HEAD)
            if not chunk:
                break
            body += chunk
        return (method, path, headers), body[:length]

    # -- routing ----------------------------------------------------------
    def _route(self, conn, method: str, path: str, headers: dict, body: bytes, state: dict) -> None:
        auth = headers.get("authorization", "")
        if auth != f"Bearer {self.api_key}":
            raise BridgeError(401, "invalid or missing bearer token")
        if path == "/health" and method == "GET":
            self._send_json(conn, 200, {"ok": True}, state=state)
        elif path == "/v1/models" and method == "GET":
            s = self.settings_fn()
            self._send_json(
                conn,
                200,
                {"object": "list", "data": [{"id": s.get("model", "gremlin"), "object": "model"}]},
                state=state,
            )
        elif path == "/v1/chat/completions" and method == "POST":
            try:
                req = json.loads(body or b"{}")
            except json.JSONDecodeError as e:
                raise BridgeError(400, f"invalid JSON body: {e}")
            if not isinstance(req, dict):
                raise BridgeError(400, "request body must be a JSON object")
            self._chat_completion(conn, req, headers, state)
        else:
            self._send_json(conn, 404, {"error": {"message": f"no route: {method} {path}"}}, state=state)

    def _chat_completion(self, conn, req: dict, headers: dict, state: dict) -> None:
        messages = req.get("messages") or []
        if not isinstance(messages, list) or not messages:
            raise BridgeError(400, "messages must be a non-empty list")
        if not isinstance(messages[-1], dict) or messages[-1].get("role") != "user":
            raise BridgeError(400, "last message must be from the user")
        stream = bool(req.get("stream"))
        settings = self.settings_fn()

        sid, prior = self._resolve_session(headers, messages)
        for m in prior:
            if isinstance(m, dict) and m.get("role") in ("user", "assistant"):
                self.sessions.add_message(
                    sid,
                    {"id": uuid.uuid4().hex, "role": m["role"], "content": m.get("content", ""), "ts": _now()},
                )

        completion_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
        created = int(time.time())
        model = settings.get("model", "gremlin")
        user_text = messages[-1].get("content", "")

        if not stream:
            text_parts: list[str] = []
            error: str | None = None
            for ev in self.manager.run(sid, user_text, settings):
                if ev["type"] == "text":
                    text_parts.append(ev["text"])
                elif ev["type"] == "error":
                    error = ev["message"]
            if error:
                raise BridgeError(502, error)
            body = {
                "id": completion_id,
                "object": "chat.completion",
                "created": created,
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "".join(text_parts)},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            }
            self._send_json(conn, 200, body, state=state, extra_headers={"X-Gremlin-Session": sid})
        else:
            self._send_json(
                conn,
                200,
                None,
                state=state,
                content_type="text/event-stream",
                extra_headers={"X-Gremlin-Session": sid, "Cache-Control": "no-cache"},
                suppress_content_length=True,
            )
            self._stream_events(conn, sid, user_text, settings, completion_id, created, model)

    def _stream_events(self, conn, sid, user_text, settings, completion_id, created, model) -> None:
        def chunk(delta: dict, finish: str | None = None) -> bytes:
            payload = {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
            }
            return f"data: {json.dumps(payload)}\n\n".encode()

        conn.sendall(chunk({"role": "assistant", "content": ""}))
        try:
            for ev in self.manager.run(sid, user_text, settings):
                if ev["type"] == "text":
                    conn.sendall(chunk({"content": ev["text"]}))
                elif ev["type"] == "error":
                    # Head already sent: no HTTP status possible. Signal the
                    # failure in-band, then terminate the stream cleanly.
                    conn.sendall(chunk({"content": f"\n(error: {ev['message']})"}))
                    break
            conn.sendall(chunk({}, "stop"))
            conn.sendall(b"data: [DONE]\n\n")
        except OSError:
            pass  # client went away; nothing left to say

    def _resolve_session(self, headers: dict, messages: list) -> tuple[str, list[dict]]:
        """Return (session_id, prior_messages_to_seed).

        With an X-Gremlin-Session header the session is reused (and must
        exist); otherwise a fresh session is created and seeded with the
        caller's earlier messages.
        """
        sid = (headers.get("x-gremlin-session") or "").strip()
        if sid:
            try:
                self.sessions.get(sid)
            except Exception:
                raise BridgeError(404, f"unknown session: {sid}")
            return sid, []
        title = (str(messages[-1].get("content") or "")[:60]) or "bridge"
        s = self.sessions.create(title)
        return s["id"], messages[:-1]

    # -- responses ---------------------------------------------------------
    def _send_json(
        self,
        conn: socket.socket,
        status: int,
        obj: dict | None,
        state: dict,
        extra_headers: dict | None = None,
        content_type: str = "application/json",
        suppress_content_length: bool = False,
    ) -> None:
        if state["headers_sent"]:
            raise BridgeError(status, "cannot send response: head already sent")
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8") if obj is not None else b""
        reason = {
            200: "OK",
            400: "Bad Request",
            401: "Unauthorized",
            404: "Not Found",
            405: "Method Not Allowed",
            413: "Payload Too Large",
            431: "Range Not Satisfiable",
            500: "Internal Server Error",
            502: "Bad Gateway",
        }.get(status, "OK")
        lines = [
            f"HTTP/1.1 {status} {reason}",
            f"Content-Type: {content_type}",
            "Connection: close",
        ]
        if not suppress_content_length:
            lines.append(f"Content-Length: {len(body)}")
        for k, v in (extra_headers or {}).items():
            lines.append(f"{k}: {v}")
        conn.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + body)
        state["headers_sent"] = True

    def _send_error(self, conn: socket.socket, status: int, message: str, state: dict) -> None:
        if state["headers_sent"]:
            # Stream already started: an HTTP status line would corrupt it.
            log.error("bridge error after head sent (%d): %s", status, message)
            return
        self._send_json(conn, status, {"error": {"message": message}}, state=state)
