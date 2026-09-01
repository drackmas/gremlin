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


COMPACT_PROMPT = (
    "You compact chat history. Reply with a concise markdown summary that preserves: "
    "user intents, decisions made, open questions, key facts and figures, file/URL "
    "references, and the state of any in-progress task. Do not answer anything in the "
    "conversation; only summarize."
)

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
    def __init__(self, cfg, sessions, registry, skills: SkillLoader, backend=None, memory=None) -> None:
        self.cfg = cfg
        self.sessions = sessions
        self.registry = registry
        self.skills = skills
        self._backend = backend
        self.memory = memory

    def compress(self, session_id: str, settings: dict) -> dict:
        """Summarize the session so far into one stored compression message."""
        session = self.sessions.get(session_id)
        history = [
            {"role": m["role"], "content": m["content"]}
            for m in session["messages"]
            if m.get("content") and m["role"] in ("user", "assistant")
        ]
        if not history:
            raise ValueError("nothing to compress")
        api = [{"role": "system", "content": COMPACT_PROMPT}] + history
        backend = self._backend or OpenAICompatBackend(settings["base_url"])
        parts: list[str] = []
        for ev in backend.stream(api, [], settings["model"]):
            if ev.kind == "text":
                parts.append(ev.text)
        summary = "".join(parts).strip()
        if not summary:
            raise ModelError("model returned an empty summary")
        msg = {
            "id": uuid.uuid4().hex,
            "role": "assistant",
            "content": summary,
            "compression": True,
            "ts": _now(),
        }
        self.sessions.add_message(session_id, msg)
        log.info("session %s compressed to %d chars", session_id, len(summary))
        return msg

    def run(self, session_id: str, user_text: str, settings: dict):
        user_msg = {
            "id": uuid.uuid4().hex,
            "role": "user",
            "content": user_text,
            "ts": _now(),
        }
        self.sessions.add_message(session_id, user_msg)

        show_thinking = bool(settings.get("show_thinking", True))
        system = build_system_prompt(
            self.skills.list(),
            identity=settings.get("identity", ""),
            now=datetime.now().astimezone().strftime("%A, %B %d, %Y, %H:%M %Z"),
            memory=[m["content"] for m in self.memory.pinned()] if self.memory is not None else None,
        )
        api_messages: list[dict] = [{"role": "system", "content": system}]
        stored = self.sessions.get(session_id)["messages"]
        cut = next((i for i in range(len(stored) - 1, -1, -1) if stored[i].get("compression")), None)
        if cut is not None:
            # Post-compaction: the summary (as a user message) replaces all earlier turns.
            api_messages.append({"role": "user", "content": stored[cut]["content"]})
            tail = stored[cut + 1:]
        else:
            tail = stored
        for m in tail:
            api_messages.extend(_stored_to_api(m))

        backend = self._backend or OpenAICompatBackend(settings["base_url"])
        tools = self.registry.to_openai_tools()
        model = settings["model"]

        content_parts: list[str] = []
        thinking_parts: list[str] = []
        tool_calls: list[dict] = []
        error: str | None = None
        timeline: list[dict] = []

        def _append(kind: str, text: str) -> None:
            if timeline and timeline[-1]["t"] == kind:
                timeline[-1]["text"] += text
            else:
                timeline.append({"t": kind, "text": text})

        for _ in range(self.cfg.MAX_TOOL_ITERATIONS):
            turn_content: list[str] = []
            turn_thinking: list[str] = []
            turn_tools: list[dict] = []
            try:
                for ev in backend.stream(api_messages, tools, model):
                    if ev.kind == "thinking":
                        turn_thinking.append(ev.text)
                        if show_thinking:
                            _append("thinking", ev.text)
                            yield {"type": "thinking", "text": ev.text}
                    elif ev.kind == "text":
                        turn_content.append(ev.text)
                        _append("text", ev.text)
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
                timeline.append({"t": "tool", "name": t["name"], "status": status})
                api_messages.append(
                    {"role": "tool", "tool_call_id": t["id"], "content": result}
                )
        else:
            error = error or "stopped: tool loop exceeded maximum iterations"

        if error and not "".join(content_parts):
            timeline.append({"t": "text", "text": f"(error: {error})"})
        assistant_msg = {
            "id": uuid.uuid4().hex,
            "role": "assistant",
            "content": "".join(content_parts),
            "ts": _now(),
        }
        if tool_calls:
            assistant_msg["tool_calls"] = tool_calls
        assistant_msg["timeline"] = timeline
        if error and not assistant_msg["content"]:
            assistant_msg["content"] = f"(error: {error})"
        self.sessions.add_message(session_id, assistant_msg)

        if error:
            yield {"type": "error", "message": error}
        yield {"type": "done"}
