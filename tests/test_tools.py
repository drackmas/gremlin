"""Tool registry: registration, validation, execution."""

from __future__ import annotations

import pytest

from tools import Tool, ToolRegistry


def make_registry():
    reg = ToolRegistry()
    reg.register(
        Tool(
            name="add",
            description="add two ints",
            parameters={
                "type": "object",
                "properties": {
                    "a": {"type": "integer"},
                    "b": {"type": "integer"},
                },
                "required": ["a", "b"],
            },
            handler=lambda args: str(args["a"] + args["b"]),
        )
    )
    reg.register(
        Tool(
            name="boom",
            description="always fails",
            parameters={"type": "object", "properties": {}, "required": []},
            handler=lambda args: 1 / 0,
        )
    )
    return reg


def test_registered_names():
    reg = make_registry()
    assert reg.names() == ["add", "boom"]


def test_to_openai_tools_shape():
    tools = make_registry().to_openai_tools()
    add = next(t for t in tools if t["function"]["name"] == "add")
    assert add["type"] == "function"
    assert add["function"]["parameters"]["required"] == ["a", "b"]
    assert "description" in add["function"]


def test_execute_ok():
    result, ok = make_registry().execute("add", {"a": 2, "b": 3})
    assert ok is True
    assert result == "5"


def test_missing_required_argument():
    result, ok = make_registry().execute("add", {"a": 1})
    assert ok is False
    assert result.startswith("ERROR")
    assert "b" in result


def test_wrong_type():
    result, ok = make_registry().execute("add", {"a": "2", "b": 3})
    assert ok is False
    assert "integer" in result


def test_bool_is_not_integer():
    result, ok = make_registry().execute("add", {"a": True, "b": 3})
    assert ok is False


def test_unknown_argument_rejected():
    result, ok = make_registry().execute("add", {"a": 1, "b": 2, "c": 3})
    assert ok is False
    assert "c" in result


def test_non_object_arguments_rejected():
    result, ok = make_registry().execute("add", [1, 2])
    assert ok is False


def test_unknown_tool():
    result, ok = make_registry().execute("nope", {})
    assert ok is False
    assert "nope" in result


def test_handler_exception_returns_structured_error():
    result, ok = make_registry().execute("boom", {})
    assert ok is False
    assert result.startswith("ERROR")


def test_register_duplicate_raises():
    reg = ToolRegistry()
    t = Tool(name="x", description="d", parameters={"type": "object"}, handler=lambda a: "ok")
    reg.register(t)
    with pytest.raises(ValueError):
        reg.register(t)
