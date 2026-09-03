"""Task tool: per-session task plan stored as JSON under data/tasks/."""

from __future__ import annotations

import json
from pathlib import Path

from .registry import SESSION_ID, Tool


def build_task_tool(cfg) -> Tool:
    def task(args: dict) -> str:
        action = args["action"]
        sid = SESSION_ID.get()
        data_dir = Path(cfg.data_dir) / "tasks"

        if action == "plan":
            items = args.get("items") or []
            if not isinstance(items, list):
                raise ValueError("items must be a list of strings")
            data_dir.mkdir(parents=True, exist_ok=True)
            path = data_dir / f"{sid}.json"
            path.write_text(json.dumps({"items": items}, indent=2), encoding="utf-8")
            return f"planned {len(items)} item(s) for session {sid}"

        elif action == "view":
            path = data_dir / f"{sid}.json"
            if not path.exists():
                return f"no task plan for session {sid}"
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                return f"corrupt task plan for session {sid}"
            items = data.get("items", [])
            if not items:
                return f"empty task plan for session {sid}"
            lines = [f"task plan for session {sid}:"]
            for i, item in enumerate(items, 1):
                lines.append(f"  {i}. {item}")
            return "\n".join(lines)

        else:
            raise ValueError(f"unknown action: {action}")

    return Tool(
        name="task",
        description=(
            "Manage the session task plan. "
            "Actions: plan (set task items), view (read current task items). "
            "State is per-session, stored at data/tasks/<session_id>.json."
        ),
        parameters={
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["plan", "view"],
                    "description": "task action to perform",
                },
                "items": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "task items to set (plan action only)",
                },
            },
            "required": ["action"],
        },
        handler=task,
    )
