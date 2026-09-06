"""Grav CMS MCP client: a single general-purpose ``grav_mcp`` tool that proxies
to a local Grav MCP server (the ``grav-mcp`` Node package) over stdio or HTTP.

Rather than modelling each of the ~70 remote Grav tools individually, this one
tool acts as a thin MCP client: it can ``list_tools``, ``call_tool``,
``list_resources`` and ``read_resource`` against whatever the remote Grav MCP
server exposes. The companion ``grav_cms`` skill teaches the model the
discover-then-call workflow.

Configuration is read from the project ``.env`` (the API key is never logged or
echoed):

  GRAV_API_URL         required (stdio)  Grav site base URL, e.g. http://127.0.0.1:8080
  GRAV_API_KEY         required (stdio)  Grav API key / token
  GRAV_MCP_TRANSPORT   optional          'stdio' (default) or 'http'
  GRAV_MCP_HTTP_URL    optional (http)   MCP endpoint, e.g. http://127.0.0.1:3100
  GRAV_MCP_COMMAND     optional (stdio)  override launch command (default: npx)
  GRAV_MCP_ARGS        optional (stdio)  extra launch args (default: "-y grav-mcp")

The connection is established lazily on first use and reused for the lifetime
of the process (a persistent background event loop runs the async ``mcp``
client). If the remote connection drops, the next call transparently
reconnects.
"""

from __future__ import annotations

import asyncio
import atexit
import json
import logging
import os
import shlex
import threading
from typing import Any

from dotenv import dotenv_values

from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamable_http_client

from .registry import Tool
from config import AppConfig

log = logging.getLogger("gremlin.tools.grav_mcp")

CONNECT_TIMEOUT = 90          # seconds: first-time spawn + MCP handshake
REQUEST_TIMEOUT = 60          # seconds: a single list/call operation
TEARDOWN_TIMEOUT = 10         # seconds: best-effort cleanup on exit / error
DEFAULT_STDIO_COMMAND = "npx"
DEFAULT_STDIO_ARGS = "-y grav-mcp"
MAX_RESULT_CHARS = 20_000     # truncate very large remote payloads
DESC_HEAD_CHARS = 200         # cap per-item description in listings


class GravError(Exception):
    """Raised for bad arguments, missing config, or remote MCP failures.

    The registry turns this into a structured ``ERROR:`` string so the model
    can react.
    """


# --- result formatting (duck-typed so it works across mcp 1.x / 2.x) -------

def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n[... truncated {len(text) - limit} more chars ...]"


def _desc(item: Any) -> str:
    d = (getattr(item, "description", None) or "").strip()
    if len(d) > DESC_HEAD_CHARS:
        d = d[:DESC_HEAD_CHARS].rstrip() + "..."
    return d


def _format_params(schema: Any) -> str:
    """Compact ``name(type)`` param list from a JSON schema (``*`` = required)."""
    if not isinstance(schema, dict):
        return ""
    props = schema.get("properties") or {}
    if not props:
        return ""
    required = set(schema.get("required") or [])
    parts = []
    for k, spec in props.items():
        typ = (spec or {}).get("type", "") if isinstance(spec, dict) else ""
        star = "*" if k in required else ""
        parts.append(f"{k}{star}({typ})" if typ else f"{k}{star}")
    return ", ".join(parts)


def _join_content(content: Any) -> str:
    parts = []
    for item in content or []:
        itype = getattr(item, "type", None)
        if itype == "text":
            parts.append(getattr(item, "text", "") or "")
        elif itype in ("image", "audio"):
            mime = getattr(item, "mimeType", None) or getattr(item, "mime_type", None)
            parts.append(f"[{itype}]" + (f" ({mime})" if mime else ""))
        elif itype == "resource_link":
            parts.append(f"[resource link: {getattr(item, 'uri', '')}]")
        elif itype == "resource":
            inner = getattr(item, "resource", None)
            text = getattr(inner, "text", None)
            parts.append(text if text else f"[embedded resource: {getattr(inner, 'uri', '')}]")
        else:
            parts.append(str(item))
    return "\n".join(p for p in parts if p)


def _format_tools(res: Any) -> str:
    tools = list(getattr(res, "tools", None) or [])
    if not tools:
        return "The Grav MCP server exposes no tools."
    lines = [f"The Grav MCP server exposes {len(tools)} tools:"]
    for t in tools:
        name = getattr(t, "name", "?")
        d = _desc(t)
        params = _format_params(getattr(t, "input_schema", None))
        line = f"- {name}: {d}".rstrip(": ")
        if params:
            line += f"  [params: {params}]"
        lines.append(line)
    return _truncate("\n".join(lines), MAX_RESULT_CHARS)


def _format_call_result(res: Any) -> str:
    structured = getattr(res, "structured_content", None)
    if isinstance(structured, (dict, list)):
        text = json.dumps(structured, indent=2, ensure_ascii=False, default=str)
    else:
        text = _join_content(getattr(res, "content", None))
    text = text or "(no content)"
    if bool(getattr(res, "is_error", False)):
        raise GravError(f"remote tool returned an error: {text}")
    return _truncate(text, MAX_RESULT_CHARS)


def _format_resources(res: Any) -> str:
    resources = list(getattr(res, "resources", None) or [])
    if not resources:
        return "The Grav MCP server exposes no resources."
    lines = [f"The Grav MCP server exposes {len(resources)} resources:"]
    for r in resources:
        uri = getattr(r, "uri", "?")
        name = getattr(r, "name", "") or ""
        d = _desc(r)
        line = f"- {uri}"
        if name and name != uri:
            line += f" ({name})"
        if d:
            line += f": {d}"
        lines.append(line)
    return _truncate("\n".join(lines), MAX_RESULT_CHARS)


def _format_read_resource(res: Any) -> str:
    parts = []
    for c in (getattr(res, "contents", None) or []):
        text = getattr(c, "text", None)
        if text:
            parts.append(text)
            continue
        blob = getattr(c, "blob", None)
        if blob:
            mime = getattr(c, "mime_type", None) or getattr(c, "mimeType", None) or "binary"
            parts.append(f"[{mime}: {len(blob)} base64 chars]")
        else:
            parts.append(str(c))
    text = "\n".join(p for p in parts if p)
    return _truncate(text or "(empty resource)", MAX_RESULT_CHARS)


# --- persistent connection -------------------------------------------------

class _GravConnection:
    """Lazily-spawned, persistent MCP client connection to the Grav server.

    The async ``mcp`` client runs on a dedicated background event loop so the
    synchronous tool handler can ``run_coroutine_threadsafe`` a request. The
    connection (and, for stdio, the Node subprocess) is created once and reused
    for the process lifetime; a failed call resets it so the next call
    reconnects.
    """

    def __init__(self, cfg: AppConfig) -> None:
        self.cfg = cfg
        self._lock = threading.Lock()        # serializes all MCP access
        self._loop_lock = threading.Lock()   # guards loop creation
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._streams_ctx: Any = None
        self._session_ctx: Any = None
        self._session: Any = None

    # -- config -----------------------------------------------------------
    def _env(self, key: str) -> str:
        val = os.environ.get(key)
        if val:
            return val.strip()
        try:
            val = dotenv_values(self.cfg.env_path).get(key)
        except Exception:
            val = None
        return (val or "").strip()

    def _transport(self) -> str:
        return (self._env("GRAV_MCP_TRANSPORT") or "stdio").strip().lower()

    def _stdio_env(self) -> dict:
        url = self._env("GRAV_API_URL")
        key = self._env("GRAV_API_KEY")
        if not url:
            raise GravError("missing GRAV_API_URL in .env (Grav site base URL, e.g. http://127.0.0.1:8080)")
        if not key:
            raise GravError("missing GRAV_API_KEY in .env")
        # Full environment so Node can resolve PATH/HOME; the Grav credentials
        # are injected here but never logged.
        env = dict(os.environ)
        env["GRAV_API_URL"] = url
        env["GRAV_API_KEY"] = key
        return env

    # -- background loop --------------------------------------------------
    def _ensure_loop(self) -> None:
        if self._loop is not None and self._loop.is_running():
            return
        with self._loop_lock:
            if self._loop is not None and self._loop.is_running():
                return
            loop = asyncio.new_event_loop()
            self._loop = loop
            self._thread = threading.Thread(
                target=self._run_loop, args=(loop,), name="grav-mcp", daemon=True
            )
            self._thread.start()

    @staticmethod
    def _run_loop(loop: asyncio.AbstractEventLoop) -> None:
        asyncio.set_event_loop(loop)
        loop.run_forever()

    def _run(self, coro: Any, timeout: float) -> Any:
        self._ensure_loop()
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return fut.result(timeout)

    # -- connection lifecycle ---------------------------------------------
    async def _connect(self) -> None:
        transport = self._transport()
        if transport == "http":
            url = self._env("GRAV_MCP_HTTP_URL")
            if not url:
                raise GravError(
                    "missing GRAV_MCP_HTTP_URL in .env (MCP endpoint, e.g. "
                    "http://127.0.0.1:3100) for http transport"
                )
            self._streams_ctx = streamable_http_client(url)
        elif transport == "stdio":
            command = self._env("GRAV_MCP_COMMAND") or DEFAULT_STDIO_COMMAND
            args = shlex.split(self._env("GRAV_MCP_ARGS") or DEFAULT_STDIO_ARGS)
            self._streams_ctx = stdio_client(
                StdioServerParameters(command=command, args=args, env=self._stdio_env())
            )
        else:
            raise GravError(f"unknown GRAV_MCP_TRANSPORT '{transport}' (expected 'stdio' or 'http')")

        read_stream, write_stream = await self._streams_ctx.__aenter__()
        self._session_ctx = ClientSession(read_stream, write_stream)
        self._session = await self._session_ctx.__aenter__()
        await self._session.initialize()
        log.info("grav-mcp connected (%s)", transport)

    async def _teardown(self) -> None:
        session_ctx, self._session_ctx = self._session_ctx, None
        self._session = None
        streams_ctx, self._streams_ctx = self._streams_ctx, None
        if session_ctx is not None:
            try:
                await session_ctx.__aexit__(None, None, None)
            except Exception:
                pass
        if streams_ctx is not None:
            try:
                await streams_ctx.__aexit__(None, None, None)
            except Exception:
                pass

    def _safe_teardown(self) -> None:
        if self._session is None and self._session_ctx is None and self._streams_ctx is None:
            return
        try:
            self._run(self._teardown(), timeout=TEARDOWN_TIMEOUT)
        except Exception:
            self._session = None
            self._session_ctx = None
            self._streams_ctx = None

    def _atexit_teardown(self) -> None:
        if self._loop is None:
            return
        self._safe_teardown()
        try:
            self._loop.call_soon_threadsafe(self._loop.stop)
        except Exception:
            pass

    # -- public sync entry ------------------------------------------------
    def call(
        self,
        action: str,
        tool_name: str | None = None,
        arguments: dict | None = None,
        uri: str | None = None,
    ) -> str:
        with self._lock:
            try:
                if self._session is None:
                    self._run(self._connect(), timeout=CONNECT_TIMEOUT)
                return self._run(self._op(action, tool_name, arguments, uri), timeout=REQUEST_TIMEOUT)
            except GravError:
                raise
            except Exception as e:
                self._safe_teardown()
                raise GravError(f"Grav MCP {action} failed: {type(e).__name__}: {e}") from e

    # -- operations -------------------------------------------------------
    async def _op(self, action: str, tool_name: str | None, arguments: dict | None, uri: str | None) -> str:
        if action == "list_tools":
            return _format_tools(await self._session.list_tools())
        if action == "call_tool":
            if not tool_name:
                raise GravError("action=call_tool requires 'tool_name'")
            return _format_call_result(await self._session.call_tool(tool_name, arguments or None))
        if action == "list_resources":
            return _format_resources(await self._session.list_resources())
        if action == "read_resource":
            if not uri:
                raise GravError("action=read_resource requires 'uri'")
            return _format_read_resource(await self._session.read_resource(uri))
        raise GravError(
            f"unknown action '{action}' (expected list_tools, call_tool, list_resources or read_resource)"
        )


def build_grav_mcp_tool(cfg: AppConfig) -> Tool:
    conn = _GravConnection(cfg)
    atexit.register(conn._atexit_teardown)

    def grav_mcp(args: dict) -> str:
        action = (args.get("action") or "").strip()
        return conn.call(
            action=action,
            tool_name=args.get("tool_name"),
            arguments=args.get("arguments"),
            uri=args.get("uri"),
        )

    return Tool(
        name="grav_mcp",
        description=(
            "Talk to the local Grav CMS through the Grav MCP server. "
            "Use action='list_tools' to discover the remote tools, then "
            "action='call_tool' with tool_name + arguments to invoke one "
            "(also 'list_resources' / 'read_resource'). "
            "Load the 'grav_cms' skill for content workflows."
        ),
        parameters={
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "description": "One of: list_tools, call_tool, list_resources, read_resource.",
                },
                "tool_name": {
                    "type": "string",
                    "description": "Remote Grav MCP tool name (required when action=call_tool).",
                },
                "arguments": {
                    "type": "object",
                    "description": (
                        "JSON arguments for the remote tool (action=call_tool). "
                        "Free-form: match the remote tool's own schema (see list_tools)."
                    ),
                },
                "uri": {
                    "type": "string",
                    "description": "Resource URI (required when action=read_resource).",
                },
            },
            "required": ["action"],
        },
        handler=grav_mcp,
    )
