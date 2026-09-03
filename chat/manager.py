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
from datetime import datetime

from typing import Iterator

from config import AppConfig
from memory.store import MemoryStore
from models.base import ModelBackend, ModelError
from models.openai_compat import OpenAICompatBackend
from sessions import SessionManager
from skills.loader import SkillLoader
from tools.registry import SESSION_ID, ToolRegistry
from utils import now_utc

from .prompts import build_system_prompt

log = logging.getLogger("gremlin.chat")


COMPACT_PROMPT = (
    "You compact chat history. Reply with a concise markdown summary that preserves: "
    "user intents, decisions made, open questions, key facts and figures, file/URL "
    "references, and the state of any in-progress task. Do not answer anything in the "
    "conversation; only summarize."
)


def _tool_call_obj(id: str, name: str, arguments: dict) -> dict:
    """Build one OpenAI assistant ``tool_calls`` entry from resolved values.

    ``arguments`` is serialized with the same ``json.dumps`` both call sites used
    inline; the value is passed through unchanged (no type check), so the wire
    output is identical for every current representation.
    """
    return {
        "id": id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }


def _tool_result_obj(tool_call_id: str, content: str) -> dict:
    """Build one OpenAI ``role: tool`` message from a tool_call_id and result."""
    return {"role": "tool", "tool_call_id": tool_call_id, "content": content}


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
            _tool_call_obj(tc.get("id") or f"call_{i}", tc.get("name", ""), tc.get("arguments", {}))
            for i, tc in enumerate(tool_calls)
        ]
    out.append(api_msg)
    for i, tc in enumerate(tool_calls):
        out.append(_tool_result_obj(tc.get("id") or f"call_{i}", tc.get("result", "")))
    return out


class ChatManager:
    def __init__(self, cfg: AppConfig, sessions: SessionManager, registry: ToolRegistry, skills: SkillLoader, backend: ModelBackend | None = None, memory: MemoryStore | None = None) -> None:
        self.cfg = cfg
        self.sessions = sessions
        self.registry = registry
        self.skills = skills
        self._backend = backend
        self.memory = memory

    def _build_api_messages(self, session_id: str, settings: dict) -> list[dict]:
        """Construct the system prompt + stored-message-to-API-message conversion."""
        system = build_system_prompt(
            self.skills.list(),
            tools=self.registry.tools(),
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
        return api_messages

    def _execute_tool_calls(self, turn_tools: list[dict], turn_content: list[str], api_messages: list[dict], timeline: list[dict]) -> tuple[list[dict], list[dict]]:
        """Execute each tool, update api_messages and timeline. Returns (events, tool_call_records)."""
        # Record the assistant's tool-call message.
        api_messages.append(
            {
                "role": "assistant",
                "content": "".join(turn_content) or "",
                "tool_calls": [
                    _tool_call_obj(t["id"], t["name"], t["arguments"])
                    for t in turn_tools
                ],
            }
        )
        events: list[dict] = []
        records: list[dict] = []
        for t in turn_tools:
            result, ok = self.registry.execute(t["name"], t["arguments"])
            status = "ok" if ok else "error"
            records.append({**t, "result": result, "status": status})
            log.info("tool call: %s -> %s", t["name"], status)
            events.append({
                "type": "tool_call",
                "name": t["name"],
                "arguments": t["arguments"],
                "result": result,
                "status": status,
            })
            timeline.append({"t": "tool", "name": t["name"], "status": status})
            api_messages.append(_tool_result_obj(t["id"], result))
        return events, records

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
            "ts": now_utc(),
        }
        self.sessions.add_message(session_id, msg)
        log.info("session %s compressed to %d chars", session_id, len(summary))
        return msg

    def _run_tool_loop(
        self,
        backend,
        api_messages,
        tools,
        model,
        show_thinking,
        timeline,
        session_id,
        state,
        max_tool_calls: int | None = None,
    ):
        """Stream model turns and execute tool calls for one user turn.

        Yields the user-facing events (``thinking`` / ``text`` /
        ``tool_call``) in order. ``api_messages`` and ``timeline`` are updated
        in place; on exit ``state`` is filled with ``content_parts``,
        ``tool_calls``, ``error`` and ``stop_reason``.
        """
        content_parts: list[str] = []
        tool_calls: list[dict] = []
        error: str | None = None
        stop_reason = "completed"

        def _append(kind: str, text: str) -> None:
            if timeline and timeline[-1]["t"] == kind:
                timeline[-1]["text"] += text
            else:
                timeline.append({"t": kind, "text": text})

        limit = max_tool_calls if max_tool_calls is not None else self.cfg._DEFAULT_TOOL_ITERATIONS
        for _ in range(limit):
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
                stop_reason = "model_error"
                log.error("model error in session %s: %s", session_id, e)
                break

            content_parts.extend(turn_content)

            if not turn_tools:
                break

            events, records = self._execute_tool_calls(turn_tools, turn_content, api_messages, timeline)
            tool_calls.extend(records)
            for ev in events:
                yield ev
        else:
            error = error or "stopped: tool loop exceeded maximum iterations"
            stop_reason = "max_iterations"

        state["content_parts"] = content_parts
        state["tool_calls"] = tool_calls
        state["error"] = error
        state["stop_reason"] = stop_reason

    def _build_assistant_message(self, content_parts, tool_calls, timeline, error) -> dict:
        """Assemble the stored assistant message from accumulated turn state."""
        if error and not "".join(content_parts):
            timeline.append({"t": "text", "text": f"(error: {error})"})
        msg = {
            "id": uuid.uuid4().hex,
            "role": "assistant",
            "content": "".join(content_parts),
            "ts": now_utc(),
        }
        if tool_calls:
            msg["tool_calls"] = tool_calls
        msg["timeline"] = timeline
        if error and not msg["content"]:
            msg["content"] = f"(error: {error})"
        return msg

    def run(self, session_id: str, user_text: str, settings: dict) -> Iterator[dict]:
        SESSION_ID.set(session_id)
        user_msg = {
            "id": uuid.uuid4().hex,
            "role": "user",
            "content": user_text,
            "ts": now_utc(),
        }
        self.sessions.add_message(session_id, user_msg)

        show_thinking = bool(settings.get("show_thinking", True))
        api_messages = self._build_api_messages(session_id, settings)
        backend = self._backend or OpenAICompatBackend(settings["base_url"])
        tools = self.registry.to_openai_tools()
        model = settings["model"]

        # read from settings first, fall back to class default
        max_tool_calls = settings.get("max_tool_calls", self.cfg._DEFAULT_TOOL_ITERATIONS)

        timeline: list[dict] = []
        state: dict = {}
        for ev in self._run_tool_loop(
            backend,
            api_messages,
            tools,
            model,
            show_thinking,
            timeline,
            session_id,
            state,
            max_tool_calls=max_tool_calls,
        ):
            yield ev

        assistant_msg = self._build_assistant_message(
            state["content_parts"], state["tool_calls"], timeline, state["error"]
        )
        self.sessions.add_message(session_id, assistant_msg)

        if state["error"]:
            yield {"type": "error", "message": state["error"]}
        yield {"type": "done", "stop_reason": state["stop_reason"]}
