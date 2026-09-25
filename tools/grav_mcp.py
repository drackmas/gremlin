"""Grav CMS MCP client: a single general-purpose ``grav_mcp`` tool that proxies
to a local Grav MCP server (the ``grav-mcp`` Node package) over stdio or HTTP.

Rather than modelling each of the ~70 remote Grav tools individually, this one
tool acts as a thin MCP client: it can ``list_tools``, ``describe_tool``,
``call_tool``, ``list_resources`` and ``read_resource`` against whatever the remote Grav MCP
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
MAX_RESULT_CHARS = 20_000     # truncate very large inline remote payloads
DESC_HEAD_CHARS = 200         # cap per-item description in resource listings
BRIEF_DESC_CHARS = 100        # cap per-tool description in the brief tool index


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


def _brief_desc(item: Any) -> str:
    """First line of the description, capped - for the brief tool index."""
    d = (getattr(item, "description", None) or "").strip()
    first = d.split("\n", 1)[0].strip()
    if len(first) > BRIEF_DESC_CHARS:
        first = first[:BRIEF_DESC_CHARS].rstrip() + "..."
    return first


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
    """Brief index of every remote tool - name + one-line description, never truncated."""
    tools = list(getattr(res, "tools", None) or [])
    if not tools:
        return "The Grav MCP server exposes no tools."
    lines = [
        f"The Grav MCP server exposes {len(tools)} tools (brief index; "
        "action='describe_tool' with tool_name returns a tool's full parameter schema):"
    ]
    for t in tools:
        lines.append(f"- {getattr(t, 'name', '?')}: {_brief_desc(t)}".rstrip(": "))
    return "\n".join(lines)


def _format_tool_detail(t: Any) -> str:
    """Full description + parameter schema for one remote tool (action=describe_tool)."""
    name = getattr(t, "name", "?")
    desc = (getattr(t, "description", None) or "").strip() or "(no description)"
    lines = [f"{name}:", "", desc]
    schema = getattr(t, "input_schema", None)
    if isinstance(schema, dict) and schema:
        params = _format_params(schema)
        if params:
            lines += ["", f"Parameters: {params}"]
        lines += [
            "",
            "Full input schema (JSON):",
            "```json",
            json.dumps(schema, indent=2, ensure_ascii=False),
            "```",
        ]
    else:
        lines += ["", "(no input schema)"]
    return "\n".join(lines)


def _call_result_text(res: Any) -> str:
    """Full, untruncated formatted text of a call_tool result."""
    structured = getattr(res, "structured_content", None)
    if isinstance(structured, (dict, list)):
        text = json.dumps(structured, indent=2, ensure_ascii=False, default=str)
    else:
        text = _join_content(getattr(res, "content", None))
    return text or "(no content)"


def _format_call_result(res: Any, result_to_file: str | None = None) -> str:
    text = _call_result_text(res)
    if bool(getattr(res, "is_error", False)):
        raise GravError(f"remote tool returned an error: {text}")
    if result_to_file:
        return _write_result_file(result_to_file, text)
    if len(text) > MAX_RESULT_CHARS:
        return _truncate(text, MAX_RESULT_CHARS) + (
            "\n[Result truncated - re-run with result_to_file to capture the full output.]"
        )
    return text


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

# --- large payload plumbing -------------------------------------------------

def _read_arguments_file(path: str) -> dict:
    """Read call_tool arguments from a JSON file (large-payload escape hatch)."""
    p = os.path.expanduser(path)
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        raise GravError(f"arguments_file not found: {p}")
    except (OSError, ValueError) as e:
        raise GravError(f"arguments_file {p} is not valid JSON: {e}")
    if not isinstance(data, dict):
        raise GravError(f"arguments_file {p} must contain a JSON object, got {type(data).__name__}")
    return data


def _write_result_file(path: str, text: str) -> str:
    """Spill full result text to a file; return a short confirmation."""
    p = os.path.expanduser(path)
    try:
        parent = os.path.dirname(p)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)
    except OSError as e:
        raise GravError(f"result_to_file: cannot write {p}: {e}")
    return f"Result written to {p} ({len(text)} chars, {len(text.splitlines())} lines)."


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
        arguments_file: str | None = None,
        result_to_file: str | None = None,
    ) -> str:
        if arguments_file:
            arguments = _read_arguments_file(arguments_file)
        with self._lock:
            try:
                if self._session is None:
                    self._run(self._connect(), timeout=CONNECT_TIMEOUT)
                return self._run(
                    self._op(action, tool_name, arguments, uri, result_to_file),
                    timeout=REQUEST_TIMEOUT,
                )
            except GravError:
                raise
            except Exception as e:
                self._safe_teardown()
                raise GravError(f"Grav MCP {action} failed: {type(e).__name__}: {e}") from e

    # -- operations -------------------------------------------------------
    async def _op(
        self,
        action: str,
        tool_name: str | None,
        arguments: dict | None,
        uri: str | None,
        result_to_file: str | None = None,
    ) -> str:
        if action == "list_tools":
            return _format_tools(await self._session.list_tools())
        if action == "describe_tool":
            if not tool_name:
                raise GravError("action=describe_tool requires 'tool_name'")
            tools = list(getattr(await self._session.list_tools(), "tools", None) or [])
            for t in tools:
                if getattr(t, "name", None) == tool_name:
                    return _format_tool_detail(t)
            raise GravError(
                f"unknown remote tool '{tool_name}'. Available: "
                + ", ".join(sorted(str(getattr(t, "name", "?")) for t in tools))
            )
        if action == "call_tool":
            if not tool_name:
                raise GravError("action=call_tool requires 'tool_name'")
            return _format_call_result(
                await self._session.call_tool(tool_name, arguments or None),
                result_to_file,
            )
        if action == "list_resources":
            return _format_resources(await self._session.list_resources())
        if action == "read_resource":
            if not uri:
                raise GravError("action=read_resource requires 'uri'")
            return _format_read_resource(await self._session.read_resource(uri))
        raise GravError(
            f"unknown action '{action}' (expected list_tools, describe_tool, call_tool, "
            "list_resources or read_resource)"
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
            arguments_file=args.get("arguments_file"),
            result_to_file=args.get("result_to_file"),
        )

    return Tool(
        name="grav_mcp",
        description=(
            "Talk to the local Grav CMS through the Grav MCP server. "
            "Use action='list_tools' for a brief index of the remote tools, "
            "action='describe_tool' with tool_name for one tool's full parameter "
            "schema, then action='call_tool' with tool_name + arguments to invoke "
            "it (also 'list_resources' / 'read_resource'). "
            "For large payloads use arguments_file (path to a JSON file of arguments) "
            "and result_to_file (path to write the full result to). "
            "Load the 'grav_cms' skill for content workflows."
        ),
        parameters={
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "description": (
                        "One of: list_tools, describe_tool, call_tool, "
                        "list_resources, read_resource."
                    ),
                },
                "tool_name": {
                    "type": "string",
                    "description": (
                        "Remote Grav MCP tool name (required when action=call_tool "
                        "or action=describe_tool)."
                    ),
                },
                "arguments": {
                    "type": "object",
                    "description": (
                        "JSON arguments for the remote tool (action=call_tool). "
                        "Free-form: match the remote tool's own schema "
                        "(see list_tools / describe_tool)."
                    ),
                },
                "arguments_file": {
                    "type": "string",
                    "description": (
                        "Path to a JSON file holding the remote tool's arguments "
                        "(action=call_tool). The client reads the file and forwards "
                        "its contents - use for large payloads instead of inlining "
                        "'arguments'. If both are given, the file wins."
                    ),
                },
                "result_to_file": {
                    "type": "string",
                    "description": (
                        "Path to write the full call_tool result to, untruncated "
                        "(action=call_tool). When set, the client returns a short "
                        "confirmation with the path instead of the result text."
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
