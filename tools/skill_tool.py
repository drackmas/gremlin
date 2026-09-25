"""The ``load_skill`` tool: lets the model read skill instructions on demand."""

from __future__ import annotations

from skills.loader import SkillNotFoundError, SkillLoader

from .registry import Tool


def build_skill_tool(loader: SkillLoader) -> Tool:
    def load_skill(args: dict) -> str:
        name = (args.get("name") or "").strip()
        try:
            return loader.get(name)
        except SkillNotFoundError:
            available = ", ".join(s["name"] for s in loader.list()) or "(none)"
            raise SkillNotFoundError(f"unknown skill '{name}'. Available skills: {available}") from None

    return Tool(
        name="load_skill",
        description="Read the full instructions of a skill by name (see available skills in the system prompt).",
        parameters={
            "type": "object",
            "properties": {"name": {"type": "string", "description": "skill name"}},
            "required": ["name"],
        },
        handler=load_skill,
    )
