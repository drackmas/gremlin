"""Chat orchestration: the model/tool loop behind one user turn.

``ChatManager.run`` yields small JSON-able dicts that the Flask layer serializes
as server-sent events:

    {"type": "thinking", "text": ...}
    {"type": "text", "text": ...}
    {"type": "tool_call", "name", "arguments", "result", "status"}
    {"type": "error", "message"}
    {"type": "done"}
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone

from models.base import ModelError
from models.openai_compat import OpenAICompatBackend
from skills.loader import SkillLoader

from .prompts import build_system_prompt

log = logging.getLogger("gremlin.chat")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _stored_to_api(message: dict) -> list[dict]:
    """Convert one stored message into OpenAI-shaped API messages."""
    role = message.get("role")
    if role == "user":
        return [{"role": "user", "content": message.get("content", "")}]
    if role != "assistant":
        return []
    out: list[dict] = []
    tool_calls = message.get("tool_calls") or []
    api_msg = {"role": "assistant", "content": message.get("content", "") or ""}
    if tool_calls:
        api_msg["tool_calls"] = [
            {
                "id": tc.get("id") or f"call_{i}",
                "type": "function",
                "function": {
                    "name": tc.get("name", ""),
                    "arguments": json.dumps(tc.get("arguments", {})),
                },
            }
            for i, tc in enumerate(tool_calls)
        ]
    out.append(api_msg)
    for i, tc in enumerate(tool_calls):
        out.append(
            {
                "role": "tool",
                "tool_call_id": tc.get("id") or f"call_{i}",
                "content": tc.get("result", ""),
            }
        )
    return out


class ChatManager:
    def __init__(self, cfg, sessions, registry, skills: SkillLoader, backend=None) -> None:
        self.cfg = cfg
        self.sessions = sessions
        self.registry = registry
        self.skills = skills
        self._backend = backend

    def run(self, session_id: str, user_text: str, settings: dict):
        user_msg = {
            "id": uuid.uuid4().hex,
            "role": "user",
            "content": user_text,
            "ts": _now(),
        }
        self.sessions.add_message(session_id, user_msg)

        show_thinking = bool(settings.get("show_thinking", True))
        system = build_system_prompt(self.skills.list())
        api_messages: list[dict] = [{"role": "system", "content": system}]
        for m in self.sessions.get(session_id)["messages"]:
            api_messages.extend(_stored_to_api(m))

        backend = self._backend or OpenAICompatBackend(settings["base_url"])
        tools = self.registry.to_openai_tools()
        model = settings["model"]

        content_parts: list[str] = []
        thinking_parts: list[str] = []
        tool_calls: list[dict] = []
        error: str | None = None

        for _ in range(self.cfg.MAX_TOOL_ITERATIONS):
            turn_content: list[str] = []
            turn_thinking: list[str] = []
            turn_tools: list[dict] = []
            try:
                for ev in backend.stream(api_messages, tools, model):
                    if ev.kind == "thinking":
                        turn_thinking.append(ev.text)
                        if show_thinking:
                            yield {"type": "thinking", "text": ev.text}
                    elif ev.kind == "text":
                        turn_content.append(ev.text)
                        yield {"type": "text", "text": ev.text}
                    elif ev.kind == "tool_call":
                        turn_tools.append(
                            {
                                "id": ev.tool_call_id,
                                "name": ev.name,
                                "arguments": ev.arguments,
                            }
                        )
            except ModelError as e:
                error = str(e)
                log.error("model error in session %s: %s", session_id, e)
                break

            content_parts.extend(turn_content)
            thinking_parts.extend(turn_thinking)

            if not turn_tools:
                break

            # Record the assistant's tool-call message, then execute each call.
            api_messages.append(
                {
                    "role": "assistant",
                    "content": "".join(turn_content) or "",
                    "tool_calls": [
                        {
                            "id": t["id"],
                            "type": "function",
                            "function": {
                                "name": t["name"],
                                "arguments": json.dumps(t["arguments"]),
                            },
                        }
                        for t in turn_tools
                    ],
                }
            )
            for t in turn_tools:
                result, ok = self.registry.execute(t["name"], t["arguments"])
                status = "ok" if ok else "error"
                tool_calls.append({**t, "result": result, "status": status})
                log.info("tool call: %s -> %s", t["name"], status)
                yield {
                    "type": "tool_call",
                    "name": t["name"],
                    "arguments": t["arguments"],
                    "result": result,
                    "status": status,
                }
                api_messages.append(
                    {"role": "tool", "tool_call_id": t["id"], "content": result}
                )
        else:
            error = error or "stopped: tool loop exceeded maximum iterations"

        assistant_msg = {
            "id": uuid.uuid4().hex,
            "role": "assistant",
            "content": "".join(content_parts),
            "ts": _now(),
        }
        if thinking_parts and show_thinking:
            assistant_msg["thinking"] = "".join(thinking_parts)
        if tool_calls:
            assistant_msg["tool_calls"] = tool_calls
        if error and not assistant_msg["content"]:
            assistant_msg["content"] = f"(error: {error})"
        self.sessions.add_message(session_id, assistant_msg)

        if error:
            yield {"type": "error", "message": error}
        yield {"type": "done"}
