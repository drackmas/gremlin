"""Tool registry: registration, argument validation, execution.

Tools are the model's only executable capabilities. Arguments come from an
untrusted model, so they are validated against each tool's JSON schema
before dispatch, and handlers are expected to return plain strings
(errors included) so the model can react to failures.
"""

from __future__ import annotations

import contextvars
import json
import logging
from dataclasses import dataclass
from typing import Any, Callable

log = logging.getLogger("gremlin.tools")

SESSION_ID: contextvars.ContextVar[str] = contextvars.ContextVar("SESSION_ID", default="default")
# Shared log format: every line carries the active session id so a session's
# activity can be grepped out of the log. ``session_id`` is injected by
# SessionFilter (defaults to "default" outside a request).
LOG_FORMAT = "%(asctime)s %(levelname)s [%(session_id)s] %(name)s: %(message)s"


class SessionFilter(logging.Filter):
    """Attach the current ``SESSION_ID`` to every log record.

    Add to a *handler* (not a logger) so it also tags records from child
    loggers that propagate to the handler.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.session_id = SESSION_ID.get()
        return True

_TYPE_MAP = {
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "object": (dict,),
    "array": (list,),
}


class ToolError(Exception):
    """Raised for unknown tools or schema violations."""


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict  # JSON schema for arguments
    handler: Callable[[dict], str]

    def to_openai(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


def validate_args(name: str, schema: dict, args: Any) -> dict:
    """Validate ``args`` against a JSON schema (nested object/array supported).

    Raises :class:`ToolError` with a precise, model-readable message. The
    top-level flat-case strings are stable and pinned by the test suite;
    nested paths use dotted notation (e.g. ``argument 'a.b'``).
    """
    if schema.get("type", "object") != "object":
        raise ToolError(f"tool {name}: unsupported schema type")
    if not isinstance(args, dict):
        raise ToolError(f"tool {name}: arguments must be an object, got {type(args).__name__}")
    props = schema.get("properties", {})
    for key in schema.get("required", []):
        if key not in args:
            raise ToolError(f"tool {name}: missing required argument '{key}'")
    for key, value in args.items():
        spec = props.get(key)
        if spec is None:
            raise ToolError(f"tool {name}: unknown argument '{key}'")
        _check_value(name, f"argument '{key}'", value, spec)
    return args


def _check_value(name: str, label: str, value: Any, spec: dict) -> None:
    """Type-check *value* against *spec*, recursing into object/array bodies.

    A spec without a ``type`` is accepted as-is (forward-compatible).
    """
    expected = spec.get("type")
    if expected is None:
        return
    py = _TYPE_MAP.get(expected)
    if py is None:
        return
    if isinstance(value, bool) and expected in ("integer", "number"):
        raise ToolError(f"tool {name}: {label} must be {expected}")
    if not isinstance(value, py):
        raise ToolError(f"tool {name}: {label} must be {expected}, got {type(value).__name__}")
    if expected == "object" and isinstance(value, dict):
        # Enforce a declared object's inner fields only when it declares
        # ``properties``; otherwise the object is free-form (used by
        # create_tool's ``parameters`` argument, whose schema the model supplies).
        if "properties" in spec:
            nprops = spec.get("properties") or {}
            for k in spec.get("required", []):
                if k not in value:
                    raise ToolError(f"tool {name}: {label} missing required field '{k}'")
            for k, v in value.items():
                s = nprops.get(k)
                if s is None:
                    raise ToolError(f"tool {name}: {label} unknown field '{k}'")
                _check_value(name, f"{label}.{k}", v, s)
    elif expected == "array" and isinstance(value, list):
        items = spec.get("items")
        if isinstance(items, dict):
            for i, v in enumerate(value):
                _check_value(name, f"{label}[{i}]", v, items)


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool, replace: bool = False) -> None:
        """Register *tool*; raise on duplicate unless ``replace=True``.

        ``replace`` swaps an existing tool of the same name (used by the
        ``create_tool`` self-extension tool to overwrite a generated tool).
        """
        if tool.name in self._tools and not replace:
            raise ValueError(f"tool already registered: {tool.name}")
        self._tools[tool.name] = tool
        log.info("tool %s: %s", "re-registered" if replace else "registered", tool.name)

    def unregister(self, name: str) -> None:
        """Remove *name* from the registry (no-op if absent)."""
        self._tools.pop(name, None)

    def has(self, name: str) -> bool:
        """True if *name* is a registered tool."""
        return name in self._tools

    # Alias kept for readability at call sites that ask about membership.
    contains = has

    def get(self, name: str) -> Tool:
        if name not in self._tools:
            raise ToolError(f"unknown tool: {name}")
        return self._tools[name]

    def names(self) -> list[str]:
        return sorted(self._tools)

    def tools(self) -> list[Tool]:
        """All registered Tool objects in sorted-name order."""
        return [self._tools[n] for n in sorted(self._tools)]

    def to_openai_tools(self) -> list[dict]:
        return [t.to_openai() for t in (self._tools[n] for n in sorted(self._tools))]

    def execute(self, name: str, args: Any) -> tuple[str, bool]:
        """Run a tool; returns (result_text, ok). Never raises for tool-level
        failures -- those are returned as structured error strings so the
        model can recover."""
        if isinstance(args, dict) and "_raw" in args:
            log.warning("tool %s got malformed JSON args: %s", name, str(args["_raw"])[:200])
            return (
                f"ERROR: tool {name} received malformed JSON arguments ({str(args['_raw'])[:200]}). "
                "Retry the call with valid JSON."
            ), False
        try:
            tool = self.get(name)
            args = validate_args(name, tool.parameters, args)
        except ToolError as e:
            log.warning("tool validation failed: %s", e)
            return f"ERROR: {e}", False
        try:
            result = tool.handler(args)
            if not isinstance(result, str):
                result = json.dumps(result, ensure_ascii=False)
            log.info("tool ok: %s", name)
            return result, True
        except Exception as e:  # handler failure -> structured error for model
            log.exception("tool failed: %s", name)
            return f"ERROR: {type(e).__name__}: {e}", False
