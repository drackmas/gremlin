"""Tool registry assembly: filesystem + grep + youtube + skill + memory + meta.

``build_registry`` wires every built-in tool set and the self-extension
("meta") tools, then re-registers any tools persisted from earlier sessions.
"""

from __future__ import annotations

from skills.loader import SkillLoader
from memory.store import MemoryStore

from .filesystem import build_fs_tools
from .registry import Tool, ToolError, ToolRegistry, validate_args
from .memory import build_memory_tools
from .skill_tool import build_skill_tool
from .web import build_web_tools
from .youtube import build_youtube_tools
from .shell import build_shell_tool
from .grep import build_grep_tool, build_find_tool
from .task import build_task_tool
from .meta import build_meta_tools, load_generated_tools

__all__ = [
    "Tool",
    "ToolError",
    "ToolRegistry",
    "validate_args",
    "build_registry",
    "build_meta_tools",
    "load_generated_tools",
    "SkillLoader",
]


def build_registry(cfg, loader: SkillLoader | None = None, memory_store: MemoryStore | None = None, settings_loader=None) -> ToolRegistry:
    registry = ToolRegistry()
    for tool in build_fs_tools(cfg, cfg.FS_READ_LIMIT):
        registry.register(tool)
    registry.register(build_shell_tool(cfg, settings_loader))
    registry.register(build_grep_tool(cfg))
    registry.register(build_find_tool(cfg))
    registry.register(build_task_tool(cfg))
    for tool in build_web_tools(cfg):
        registry.register(tool)
    for tool in build_youtube_tools(cfg):
        registry.register(tool)
    loader = loader or SkillLoader(cfg)
    registry.register(build_skill_tool(loader))
    for tool in build_memory_tools(memory_store or MemoryStore(cfg.data_dir / "memory.json")):
        registry.register(tool)
    # Self-extension: the model can list/create skills and tools on demand.
    for tool in build_meta_tools(cfg, loader, registry):
        registry.register(tool)
    # Re-register tools persisted from earlier sessions (never clobber built-ins).
    load_generated_tools(cfg, registry)
    return registry
