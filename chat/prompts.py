"""System prompt construction."""

from __future__ import annotations


def build_system_prompt(skill_index: list[dict], tools: list | None = None, identity: str = "", now: str = "", memory: list[str] | None = None) -> str:
    parts = [
        "You are Gremlin, a concise, helpful assistant running on local hardware. "
        "You are a general-purpose companion: casual conversation, questions, planning, "
        "and hands-on work on the user's project all count. Do not force tools onto "
        "chit-chat, but reach for them whenever they make you more accurate.",
        "You work in a loop of action and observation: call a tool, read its result, "
        "decide the next step, and repeat until the task is genuinely complete. "
        "You have tools to read, write and rename files in the user's project, run "
        "shell commands to build, test and verify, search the web, and recall memory. "
        "Paths are relative to the project root. Never invent file contents or command "
        "output - call the tool and read what it actually returns.",
        "Working well:\n"
        "- Make progress in small, verifiable steps; do not assume an action worked.\n"
        "- After changing a file or running code, verify it: re-read the file, or run "
        "the relevant test or command with run_command and check the exit code.\n"
        "- Do not stop after the first successful tool call if the task still needs "
        "work or verification - keep going until you are sure it is done, then stop.\n"
        "- If a tool fails, read the error (stdout, stderr, exit code, or the message), "
        "fix the arguments or the underlying problem, and retry. Distinguish a "
        "recoverable error (fix and continue) from a genuine blocker (explain it "
        "clearly, state what you tried, and stop).",
    ]
    if skill_index:
        lines = ["Available skills (call load_skill with the skill name to read its full instructions before doing the job):"]
        for s in skill_index:
            desc = f": {s['description']}" if s.get("description") else ""
            lines.append(f"- {s['name']}{desc}")
        parts.append("\n".join(lines))
    if tools:
        lines = [
            "Available tools (this is the complete list of tools you can call; "
            "when asked which tools you have, list exactly these names):",
        ]
        for t in tools:
            desc = (getattr(t, "description", "") or "").strip()
            first = desc.split(". ")[0].rstrip(".")
            lines.append(f"- {t.name}: {first}" if first else f"- {t.name}")
        parts.append("\n".join(lines))
    if identity:
        parts.append(f"# Identity\n{identity}")
    if now:
        parts.append(f"# Time\nCurrent time: {now}")
    if memory:
        parts.append("# Pinned memory\n" + "\n".join(f"{i}. {c}" for i, c in enumerate(memory, 1)))
    return "\n\n".join(parts)
