"""Self-extension tools: let the model create new skills and tools on demand.

This is Gremlin's "meta" capability. The model can:

- ``list_tools``   — inspect the currently registered tools (schemas +
  descriptions) so it can decide whether an existing tool already does the job.
- ``create_skill`` — write a new ``skills/<slug>/SKILL.md`` (instructions only).
  Use when a repeatable job can be done by *combining existing tools*.
- ``create_tool``  — write + register a new executable tool from Python source.
  Use only when the job *cannot* be done with the existing tools.

``create_tool`` persists to ``data/generated_tools/<name>.py``;
``load_generated_tools`` re-registers any persisted tools at startup, so a
tool created in one session survives into the next.
"""

from __future__ import annotations

import importlib.util
import logging
import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from utils import atomic_write_text

from config import AppConfig
from .registry import Tool, ToolRegistry

if TYPE_CHECKING:
    from skills.loader import SkillLoader

log = logging.getLogger("gremlin.meta")

_SLUG_RE = re.compile(r"[^a-z0-9_]+")
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def safe_slug(name: str) -> str:
    """Turn an arbitrary name into a safe filesystem slug (lowercase)."""
    slug = _SLUG_RE.sub("_", (name or "").strip().lower()).strip("_")
    if not slug:
        raise ValueError("name must contain at least one letter or digit")
    return slug[:48]


def _import_generated(path: Path, name: str) -> Any:
    """Import a generated tool module by file path (unique, self-cleaning name).

    The module is dropped from ``sys.modules`` after exec; the returned
    functions keep their globals alive via closure, so the handler stays valid.
    """
    mod_name = f"_gremlin_generated_{name}"
    spec = importlib.util.spec_from_file_location(mod_name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(mod_name, None)
    return module


def _format_tool_line(tool: Tool) -> str:
    props = tool.parameters.get("properties", {}) or {}
    required = set(tool.parameters.get("required", []) or [])
    args = ", ".join(f"{k}{'*' if k in required else ''}" for k in props)
    return f"- {tool.name}({args}): {tool.description}"


def build_meta_tools(cfg: AppConfig, loader: SkillLoader, registry: ToolRegistry) -> list[Tool]:
    """Return the ``list_tools`` / ``create_skill`` / ``create_tool`` tools."""
    skills_dir = Path(cfg.skills_dir)

    def list_tools(_args: dict) -> str:
        return "\n".join(_format_tool_line(t) for t in registry.tools()) or "(no tools)"

    def create_skill(args: dict) -> str:
        try:
            slug = safe_slug(args.get("name") or "")
        except ValueError as e:
            return f"ERROR: {e}"
        description = (args.get("description") or "").strip()
        instructions = (args.get("instructions") or "").strip()
        overwrite = bool(args.get("overwrite", False))
        if not description:
            return "ERROR: description is required"
        if not instructions:
            return "ERROR: instructions are required"
        target = skills_dir / slug / "SKILL.md"
        if target.exists() and not overwrite:
            return (
                f"ERROR: skill '{slug}' already exists. Read it with "
                f"load_skill(name='{slug}'), then call create_skill again with "
                "overwrite=true to replace it."
            )
        text = f"---\nname: {slug}\ndescription: {description}\n---\n\n{instructions}\n"
        target.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(target, text)
        loader.reload()
        log.info("created skill %s at %s", slug, target)
        return f"Created skill '{slug}'. Load it with load_skill(name='{slug}')."

    def create_tool(args: dict) -> str:
        name = (args.get("name") or "").strip()
        if not _IDENT_RE.match(name):
            return (
                "ERROR: name must be a valid Python identifier "
                "(letters/digits/underscore, not starting with a digit)"
            )
        description = (args.get("description") or "").strip()
        code = (args.get("code") or "").strip()
        # Optional overrides for the Tool the code builds; the Tool itself is
        # the source of truth when these are omitted.
        parameters = args.get("parameters")
        overwrite = bool(args.get("overwrite", False))
        if not code:
            return "ERROR: code is required"
        if parameters is not None and (
            not isinstance(parameters, dict) or parameters.get("type") != "object"
        ):
            return "ERROR: parameters must be a JSON object schema with type='object'"
        if not overwrite and registry.has(name):
            return f"ERROR: tool '{name}' already exists. Pass overwrite=true to replace it."
        try:
            compile(code + "\n", f"{name}.py", "exec")
        except SyntaxError as e:
            return f"ERROR: code has a syntax error (line {e.lineno}): {e.msg}"

        gen_dir = Path(cfg.generated_tools_dir)
        gen_dir.mkdir(parents=True, exist_ok=True)
        path = gen_dir / f"{name}.py"
        atomic_write_text(path, code + "\n")
        try:
            module = _import_generated(path, name)
            build_fn = getattr(module, "build_tool")
            tool = build_fn()
            if not isinstance(tool, Tool):
                raise ValueError("build_tool() must return a tools.registry.Tool instance")
            tool.name = name
            if description:
                tool.description = description
            if parameters is not None:
                tool.parameters = parameters
            if not (tool.description or "").strip():
                raise ValueError("the tool must have a non-empty description")
            if not isinstance(tool.parameters, dict) or tool.parameters.get("type") != "object":
                raise ValueError(
                    "the tool's parameters must be a JSON object schema with type='object'"
                )
        except Exception as e:
            # Do not leave an orphan module that load_generated_tools would load.
            path.unlink(missing_ok=True)
            log.warning("create_tool failed for %s: %s: %s", name, type(e).__name__, e)
            return f"ERROR: could not create tool '{name}': {e}"
        registry.register(tool, replace=True)
        log.info("created tool %s (persisted to %s)", name, path)
        return f"Created and registered tool '{name}'. Available now and on restart."

    return [
        Tool(
            name="list_tools",
            description="List the currently registered tools with their argument schemas and descriptions.",
            parameters={"type": "object", "properties": {}, "required": []},
            handler=list_tools,
        ),
        Tool(
            name="create_skill",
            description=(
                "Create a new skill (instructions only) at skills/<slug>/SKILL.md. Use when a "
                "repeatable job can be done by combining EXISTING tools — no new code needed. "
                "Provide a short name, a one-line description, and step-by-step instructions."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "short skill name (e.g. youtube_to_mp3)"},
                    "description": {"type": "string", "description": "one-line description of when/how to use it"},
                    "instructions": {"type": "string", "description": "full step-by-step instructions for the model"},
                    "overwrite": {"type": "boolean", "description": "replace the skill if it already exists (default false)"},
                },
                "required": ["name", "description", "instructions"],
            },
            handler=create_skill,
        ),
        Tool(
            name="create_tool",
            description=(
                "Create and register a NEW executable tool from Python source. Use only when the "
                "job CANNOT be done with the existing tools. The code MUST define build_tool() "
                "returning a tools.registry.Tool; it is syntax-checked, imported, registered now, "
                "and persisted to data/generated_tools/ for restarts."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "tool name; a valid Python identifier (snake_case)"},
                    "description": {
                        "type": "string",
                        "description": "optional one-line description (overrides the Tool's own if given)",
                    },
                    "parameters": {
                        "type": "object",
                        "description": "optional argument schema (overrides the Tool's own if given)",
                    },
                    "code": {
                        "type": "string",
                        "description": "full Python module source; MUST define build_tool() returning a Tool",
                    },
                    "overwrite": {"type": "boolean", "description": "replace the tool if it already exists (default false)"},
                },
                "required": ["name", "code"],
            },
            handler=create_tool,
        ),
    ]


def load_generated_tools(cfg, registry) -> int:
    """Re-register persisted tools from ``data/generated_tools``. Returns count.

    Unloadable files are skipped with a warning (one bad tool must not break
    startup).
    """
    gen_dir = Path(cfg.generated_tools_dir)
    if not gen_dir.is_dir():
        return 0
    count = 0
    for path in sorted(gen_dir.glob("*.py")):
        try:
            module = _import_generated(path, path.stem)
            tool = module.build_tool()
            if not isinstance(tool, Tool):
                continue
            tool.name = path.stem
            # Never clobber a built-in tool with a stale persisted file.
            if registry.has(tool.name):
                log.warning("generated tool %s shadows a built-in; skipping", tool.name)
                continue
            registry.register(tool)
            count += 1
        except Exception as e:
            log.warning("skipping generated tool %s: %s: %s", path.name, type(e).__name__, e)
    return count
