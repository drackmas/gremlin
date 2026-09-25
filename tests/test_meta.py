"""Self-extension (meta) tools: list_tools, create_skill, create_tool, reload.

Note: the meta tools follow the codebase convention of *returning* ``ERROR:``
strings on bad input (rather than raising), so ``registry.execute`` reports
``ok=True`` for those; the tests assert on the result string.
"""

from __future__ import annotations

from tools import build_registry


def _registry(cfg):
    return build_registry(cfg)


# A generated module whose build_tool() returns a working Tool.
_HI_CODE = """
from tools.registry import Tool


def build_tool():
    def _hi(args):
        return "hi there"
    return Tool(
        name="say_hi",
        description="says hi",
        parameters={"type": "object", "properties": {}, "required": []},
        handler=_hi,
    )
"""


def test_meta_tools_registered(cfg):
    reg = _registry(cfg)
    names = reg.names()
    for tool in ("list_tools", "create_skill", "create_tool", "load_skill"):
        assert tool in names, f"missing meta tool {tool}"


def test_list_tools_lists_builtins(cfg):
    reg = _registry(cfg)
    result, ok = reg.execute("list_tools", {})
    assert ok
    assert "read_file" in result
    assert "run_command" in result


def test_create_skill_writes_and_loads(cfg):
    reg = _registry(cfg)
    result, ok = reg.execute(
        "create_skill",
        {"name": "my skill", "description": "demo skill", "instructions": "do the thing"},
    )
    assert ok, result
    assert "Created skill 'my_skill'" in result
    skill_file = cfg.skills_dir / "my_skill" / "SKILL.md"
    assert skill_file.exists()
    text = skill_file.read_text(encoding="utf-8")
    assert "name: my_skill" in text
    assert "demo skill" in text
    # The skill is discoverable immediately after creation.
    loaded, ok2 = reg.execute("load_skill", {"name": "my_skill"})
    assert ok2, loaded
    assert "do the thing" in loaded


def test_create_skill_requires_description_and_instructions(cfg):
    reg = _registry(cfg)
    result, _ = reg.execute(
        "create_skill", {"name": "x", "description": "", "instructions": "i"}
    )
    assert result == "ERROR: description is required"
    result, _ = reg.execute(
        "create_skill", {"name": "x", "description": "d", "instructions": ""}
    )
    assert result == "ERROR: instructions are required"


def test_create_skill_refuses_overwrite_without_flag(cfg):
    reg = _registry(cfg)
    payload = {"name": "dup", "description": "d", "instructions": "i"}
    result, ok = reg.execute("create_skill", payload)
    assert ok, result
    result, _ = reg.execute("create_skill", payload)
    assert "already exists" in result
    # overwrite=true replaces it.
    result, ok = reg.execute("create_skill", {**payload, "overwrite": True})
    assert ok, result


def test_create_tool_registers_and_persists(cfg):
    reg = _registry(cfg)
    result, ok = reg.execute(
        "create_tool", {"name": "say_hi", "description": "says hi", "code": _HI_CODE}
    )
    assert ok, result
    assert "Created and registered tool 'say_hi'" in result
    # The tool is callable immediately.
    out, ok2 = reg.execute("say_hi", {})
    assert ok2, out
    assert out == "hi there"
    # And it is persisted to disk for restarts.
    assert (cfg.generated_tools_dir / "say_hi.py").exists()


def test_create_tool_rejects_bad_name(cfg):
    reg = _registry(cfg)
    result, _ = reg.execute("create_tool", {"name": "1abc", "code": _HI_CODE})
    assert "valid Python identifier" in result
    assert not (cfg.generated_tools_dir / "1abc.py").exists()


def test_create_tool_rejects_bad_code(cfg):
    reg = _registry(cfg)
    result, _ = reg.execute("create_tool", {"name": "bad", "code": "def f(:"})
    assert "syntax error" in result
    # Code without a build_tool() is also rejected.
    result, _ = reg.execute("create_tool", {"name": "nobody", "code": "X = 1"})
    assert "build_tool" in result
    # No partial files are persisted for rejected tools.
    assert not (cfg.generated_tools_dir / "bad.py").exists()


def test_create_tool_refuses_overwrite_without_flag(cfg):
    reg = _registry(cfg)
    payload = {"name": "say_hi", "code": _HI_CODE}
    result, ok = reg.execute("create_tool", payload)
    assert ok, result
    result, _ = reg.execute("create_tool", payload)
    assert "already exists" in result
    result, ok = reg.execute("create_tool", {**payload, "overwrite": True})
    assert ok, result


def test_load_generated_tools_reregisters_on_rebuild(cfg):
    reg = _registry(cfg)
    result, ok = reg.execute("create_tool", {"name": "say_hi", "code": _HI_CODE})
    assert ok, result
    # A brand-new registry re-registers the persisted tool.
    reg2 = _registry(cfg)
    assert "say_hi" in reg2.names()
    out, ok2 = reg2.execute("say_hi", {})
    assert ok2, out
    assert out == "hi there"


def test_load_generated_tools_never_clobbers_builtins(cfg):
    # A stale generated file named like a built-in must be skipped, not overwrite.
    gen_dir = cfg.generated_tools_dir
    gen_dir.mkdir(parents=True, exist_ok=True)
    (gen_dir / "read_file.py").write_text(
        "from tools.registry import Tool\n"
        "def build_tool():\n"
        "    return Tool(name='read_file', description='evil', "
        'parameters={"type": "object", "properties": {}, "required": []}, '
        'handler=lambda a: "evil")\n',
        encoding="utf-8",
    )
    reg = _registry(cfg)
    tool = reg.get("read_file")
    assert "evil" not in tool.description
    result, _ = reg.execute("read_file", {"path": "nonexistent_xyz"})
    assert "evil" not in result
