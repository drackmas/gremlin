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


# --- task tool -------------------------------------------------------------


def _task_registry(tmp_path):
    from config import AppConfig, ensure_dirs
    from tools import build_registry

    cfg = AppConfig(root=tmp_path)
    ensure_dirs(cfg)
    return build_registry(cfg)


def test_task_plan_writes_file(tmp_path):
    from tools.registry import SESSION_ID

    reg = _task_registry(tmp_path)
    SESSION_ID.set("test-sid-1")
    out, ok = reg.execute("task", {"action": "plan", "items": ["do a", "do b"]})
    assert ok, out
    assert "planned 2 item(s)" in out
    path = tmp_path / "data" / "tasks" / "test-sid-1.json"
    assert path.exists()


def test_task_view_reads_file(tmp_path):
    from tools.registry import SESSION_ID

    reg = _task_registry(tmp_path)
    SESSION_ID.set("test-sid-2")
    reg.execute("task", {"action": "plan", "items": ["step one", "step two"]})
    out, ok = reg.execute("task", {"action": "view"})
    assert ok, out
    assert "step one" in out
    assert "step two" in out
    assert "task plan for session test-sid-2" in out


def test_task_sessions_independent(tmp_path):
    from tools.registry import SESSION_ID

    reg = _task_registry(tmp_path)
    SESSION_ID.set("sid-a")
    reg.execute("task", {"action": "plan", "items": ["only a"]})
    SESSION_ID.set("sid-b")
    reg.execute("task", {"action": "plan", "items": ["only b"]})

    SESSION_ID.set("sid-a")
    out_a, _ = reg.execute("task", {"action": "view"})
    assert "only a" in out_a
    assert "only b" not in out_a

    SESSION_ID.set("sid-b")
    out_b, _ = reg.execute("task", {"action": "view"})
    assert "only b" in out_b
    assert "only a" not in out_b


def test_task_view_same_sid_shares(tmp_path):
    from tools.registry import SESSION_ID

    reg = _task_registry(tmp_path)
    SESSION_ID.set("shared-sid")
    reg.execute("task", {"action": "plan", "items": ["x", "y"]})
    out1, _ = reg.execute("task", {"action": "view"})
    out2, _ = reg.execute("task", {"action": "view"})
    assert out1 == out2


def test_task_view_no_plan(tmp_path):
    from tools.registry import SESSION_ID

    reg = _task_registry(tmp_path)
    SESSION_ID.set("empty-sid")
    out, ok = reg.execute("task", {"action": "view"})
    assert ok, out
    assert "no task plan" in out


def test_session_filter_injects_session_id():
    import logging

    from tools.registry import LOG_FORMAT, SESSION_ID, SessionFilter

    lines = []

    class _Cap(logging.Handler):
        def emit(self, record):
            lines.append(self.format(record))

    handler = _Cap()
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    handler.addFilter(SessionFilter())
    logger = logging.getLogger("test.session.filter")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False

    # The filter injects whatever session id is active at emit time, so set
    # known values explicitly (do not depend on prior test state).
    try:
        SESSION_ID.set("sid-a")
        logger.info("phase a")
        SESSION_ID.set("sid-b")
        logger.info("phase b")
    finally:
        logger.removeHandler(handler)

    assert "[sid-a]" in lines[0]
    assert "[sid-b]" in lines[1]
