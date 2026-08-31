"""Tool registry assembly: filesystem tools + skill tool."""

from __future__ import annotations

from skills.loader import SkillLoader

from .filesystem import build_fs_tools
from .registry import Tool, ToolError, ToolRegistry, validate_args
from .skill_tool import build_skill_tool
from .youtube import build_youtube_tools

__all__ = [
    "Tool",
    "ToolError",
    "ToolRegistry",
    "validate_args",
    "build_registry",
    "SkillLoader",
]


def build_registry(cfg, loader: SkillLoader | None = None) -> ToolRegistry:
    registry = ToolRegistry()
    for tool in build_fs_tools(cfg, cfg.FS_READ_LIMIT):
        registry.register(tool)
    for tool in build_youtube_tools(cfg):
        registry.register(tool)
    registry.register(build_skill_tool(loader or SkillLoader(cfg)))
    return registry
