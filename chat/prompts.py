"""System prompt construction."""

from __future__ import annotations


def build_system_prompt(skill_index: list[dict], identity: str = "", now: str = "", memory: list[str] | None = None) -> str:
    parts = [
        "You are Gremlin, a concise, helpful assistant running on local hardware.",
        "You can call tools to inspect and modify files inside the user's project "
        "workspace. Use tools whenever they help you answer; never invent file "
        "contents. Paths passed to tools are relative to the project root.",
        "Keep answers direct and well-structured. If a tool call fails, read the "
        "error, fix the arguments, and retry once before giving up.",
    ]
    if skill_index:
        lines = ["Available skills (call load_skill with the skill name to read its full instructions before doing the job):"]
        for s in skill_index:
            desc = f": {s['description']}" if s.get("description") else ""
            lines.append(f"- {s['name']}{desc}")
        parts.append("\n".join(lines))
    if identity:
        parts.append(f"# Identity\n{identity}")
    if now:
        parts.append(f"# Time\nCurrent time: {now}")
    if memory:
        parts.append("# Pinned memory\n" + "\n".join(f"{i}. {c}" for i, c in enumerate(memory, 1)))
    return "\n\n".join(parts)
