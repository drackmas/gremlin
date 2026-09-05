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

# ---------------------------------------------------------------------------
# Prompts & constants
# ---------------------------------------------------------------------------

COMPACT_PROMPT = (
    "You are a conversation compactor. Summarize the chat history above into a "
    "structured markdown block that a future model turn can use as context. "
    "Your summary MUST include these sections (omit a section only if truly empty):\n\n"
    "## User Goals & Decisions\n"
    "What the user asked for, key decisions made, constraints stated.\n\n"
    "## Important Tool Outcomes\n"
    "One-line summaries of significant tool results (file writes, command outputs, "
    "searches). Do NOT paste full outputs; capture the essential fact.\n\n"
    "## Current State\n"
    "What was being worked on last, any open questions, pending tasks.\n\n"
    "## Key Facts & References\n"
    "File paths, URLs, names, values the model still needs.\n\n"
    "Rules: be concise (under 500 words total). Do not answer the user's questions "
    "or continue the conversation — only summarize for context handoff."
)

# Rough default chars-per-token estimate (conservative ~4 chars/token).
# Overridable per request via the ``chars_per_token`` setting.
_DEFAULT_CHARS_PER_TOKEN = 4


def _chars_per_token(settings: dict | None) -> int:
    """Resolve the chars-per-token divisor from settings (default 4)."""
    if settings:
        try:
            value = int(settings.get("chars_per_token", _DEFAULT_CHARS_PER_TOKEN))
            return value if value > 0 else _DEFAULT_CHARS_PER_TOKEN
        except (TypeError, ValueError):
            pass
    return _DEFAULT_CHARS_PER_TOKEN


def _estimate_tokens(text: str, chars_per_token: int = _DEFAULT_CHARS_PER_TOKEN) -> int:
    """Lightweight token estimate: chars / chars_per_token. Good for budgeting."""
    return max(1, len(text) // max(1, chars_per_token))


def _estimate_messages_tokens(
    messages: list[dict], chars_per_token: int = _DEFAULT_CHARS_PER_TOKEN
) -> int:
    """Sum token estimates across all message content fields."""
    total = 0
    for m in messages:
        content = m.get("content") or ""
        total += _estimate_tokens(content, chars_per_token)
        for tc in m.get("tool_calls") or []:
            total += _estimate_tokens(json.dumps(tc.get("arguments", {})), chars_per_token)
    return total


def _truncate_tool_result(result: str, max_chars: int) -> str:
    """Truncate a tool result to max_chars, appending a truncation marker."""
    if len(result) <= max_chars:
        return result
    return result[:max_chars] + f"\n... [truncated: {len(result) - max_chars} chars omitted]"


# ---------------------------------------------------------------------------
# OpenAI message helpers
# ---------------------------------------------------------------------------


def _tool_call_obj(id: str, name: str, arguments: dict) -> dict:
    """Build one OpenAI assistant ``tool_calls`` entry from resolved values."""
    return {
        "id": id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }


def _tool_result_obj(tool_call_id: str, content: str) -> dict:
    """Build one OpenAI ``role: tool`` message from a tool_call_id and result."""
    return {"role": "tool", "tool_call_id": tool_call_id, "content": content}


def _stored_to_api(message: dict, max_tool_chars: int = 8000) -> list[dict]:
    """Convert one stored message into OpenAI-shaped API messages.

    Tool results are truncated to ``max_tool_chars`` to keep the API payload
    bounded. The full result remains in the session file for history purposes.
    """
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
        result = tc.get("result", "")
        out.append(_tool_result_obj(tc.get("id") or f"call_{i}", _truncate_tool_result(result, max_tool_chars)))
    return out


def _find_last_compression(stored: list[dict]) -> int | None:
    """Return index of the last compression summary message, or None."""
    for i in range(len(stored) - 1, -1, -1):
        if stored[i].get("compression"):
            return i
    return None


def _group_into_turns(stored: list[dict]) -> list[list[dict]]:
    """Group stored messages into conversation turns.

    A turn is one user message + one assistant message (with any tool calls).
    This lets us bound context by number of turns rather than raw messages.
    """
    turns: list[list[dict]] = []
    current: list[dict] = []
    for msg in stored:
        role = msg.get("role")
        if role == "user" and current:
            # New user message starts a new turn
            turns.append(current)
            current = []
        current.append(msg)
    if current:
        turns.append(current)
    return turns


# ---------------------------------------------------------------------------
# ChatManager
# ---------------------------------------------------------------------------


class ChatManager:
    def __init__(self, cfg: AppConfig, sessions: SessionManager, registry: ToolRegistry, skills: SkillLoader, backend: ModelBackend | None = None, memory: MemoryStore | None = None) -> None:
        self.cfg = cfg
        self.sessions = sessions
        self.registry = registry
        self.skills = skills
        self._backend = backend
        self.memory = memory

    # -- context building ---------------------------------------------------

    def _build_system(self, settings: dict) -> str:
        """Build the system prompt (always included, never truncated)."""
        return build_system_prompt(
            self.skills.list(),
            tools=self.registry.tools(),
            identity=settings.get("identity", ""),
            now=datetime.now().astimezone().strftime("%A, %B %d, %Y, %H:%M %Z"),
            memory=[m["content"] for m in self.memory.pinned()] if self.memory is not None else None,
        )

    def _build_api_messages(self, session_id: str, settings: dict) -> list[dict]:
        """Construct a bounded API message list.

        Strategy:
        1. System prompt (always included).
        2. Compression summary if one exists (replaces all older history).
        3. Most recent N turns verbatim (configurable, default 10).
        4. Token-aware truncation: if the total still exceeds the token budget,
           drop oldest turns until it fits.
        """
        max_context_tokens = int(settings.get("max_context_tokens", 32768))
        window_turns = int(settings.get("context_window_turns", 10))
        max_tool_chars = int(settings.get("tool_result_max_chars", 8000))

        system = self._build_system(settings)
        api_messages: list[dict] = [{"role": "system", "content": system}]

        stored = self.sessions.get(session_id)["messages"]

        # If a compression summary exists, it replaces all earlier messages.
        cut = _find_last_compression(stored)
        if cut is not None:
            # Include the summary as a system message (cleaner than user hack).
            api_messages.append({"role": "system", "content": f"[Conversation Summary]\n{stored[cut]['content']}"})
            tail = stored[cut + 1:]
        else:
            tail = stored

        # Group into turns and keep only the most recent N.
        turns = _group_into_turns(tail)
        if len(turns) > window_turns:
            dropped = len(turns) - window_turns
            log.debug("context: dropping %d oldest turns (window=%d)", dropped, window_turns)
            turns = turns[-window_turns:]

        # Flatten turns back to messages and convert to API format.
        for msg in (m for turn in turns for m in turn):
            api_messages.extend(_stored_to_api(msg, max_tool_chars))

        # Token-aware safety net: drop oldest messages if still over budget.
        # If the budget is non-positive (system prompt alone exceeds the window),
        # skip token truncation — the window-based limit above is the only useful bound.
        system_tokens = _estimate_tokens(system, _chars_per_token(settings))
        budget = max_context_tokens - system_tokens - 512  # reserve for response
        if budget > 0:
            while len(api_messages) > 1:
                msg_tokens = _estimate_messages_tokens(api_messages[1:], _chars_per_token(settings))
                if msg_tokens <= budget:
                    break
                # Drop the oldest message. If it's an assistant with tool_calls,
                # also drop its tool-result messages to keep the sequence valid
                # for the model's chat template.
                drop_end = 1
                if api_messages[1].get("role") == "assistant" and api_messages[1].get("tool_calls"):
                    i = 2
                    while i < len(api_messages) and api_messages[i].get("role") == "tool":
                        i += 1
                    drop_end = i
                del api_messages[1:drop_end]
                log.debug("context: token budget exceeded, dropping %d message(s) (est=%d, budget=%d)", drop_end - 1, msg_tokens, budget)

        return api_messages

    # -- compaction ---------------------------------------------------------

    def _should_compact(self, session_id: str, settings: dict) -> bool:
        """Check if the session's context is close enough to the window to compact.

        Prefers the real prompt-token usage from the last model request
        (recorded on the session); falls back to a chars/4 estimate of the
        stored messages when the provider reported nothing.
        """
        max_context_tokens = int(settings.get("max_context_tokens", 32768))
        threshold = float(settings.get("compaction_threshold", 0.65))
        budget = int(max_context_tokens * threshold)

        session = self.sessions.get(session_id)
        stored = session["messages"]
        cut = _find_last_compression(stored)
        # Only consider messages after the last compression.
        relevant = stored[cut + 1:] if cut is not None else stored

        # Real usage from the last model request, when the provider reported it.
        usage = session.get("last_usage") or {}
        last_prompt = usage.get("prompt_tokens")
        if isinstance(last_prompt, int) and last_prompt >= budget:
            return True
        est = _estimate_messages_tokens([
            {"role": m.get("role"), "content": m.get("content", ""), "tool_calls": m.get("tool_calls")}
            for m in relevant
        ], _chars_per_token(settings))
        return est > budget

    def _maybe_compact(self, session_id: str, settings: dict) -> None:
        """Trigger automatic compaction if the context is too large.

        Runs the model to produce a summary, stores it as a compression marker,
        and the next _build_api_messages will use it to bound history.
        """
        if not self._should_compact(session_id, settings):
            return

        log.info("session %s: auto-compaction triggered", session_id)
        try:
            self.compress(session_id, settings)
            log.info("session %s: auto-compaction complete", session_id)
        except (ModelError, Exception) as e:
            # Compaction failure should not break the turn; log and continue.
            log.warning("session %s: auto-compaction failed (%s); continuing with full context", session_id, e)

    def compress(self, session_id: str, settings: dict) -> dict:
        """Produce a high-quality summary of the session history.

        Stores the summary as a message with ``compression: True``. The
        ``_build_api_messages`` method uses this as a boundary: everything
        before it is replaced by the summary.
        """
        stored = self.sessions.get(session_id)["messages"]
        cut = _find_last_compression(stored)
        # Summarize everything after the last compression (or all if none).
        relevant = stored[cut + 1:] if cut is not None else stored

        if not relevant:
            raise ModelError("nothing to compress")

        # Build a compact version of the messages for the compaction call.
        # Limit input to the model to avoid blowing the context during compaction itself.
        max_input_tokens = int(settings.get("max_context_tokens", 32768)) // 2
        compaction_messages: list[dict] = []
        running_tokens = 0
        # Walk backwards to keep the most recent messages within budget.
        for msg in reversed(relevant):
            msg_dict = {"role": msg.get("role"), "content": msg.get("content", "")}
            if msg.get("tool_calls"):
                msg_dict["tool_calls"] = msg["tool_calls"]
            t = _estimate_messages_tokens([msg_dict], _chars_per_token(settings))
            if running_tokens + t > max_input_tokens:
                break
            compaction_messages.insert(0, msg_dict)
            running_tokens += t

        # Build the compaction API call.
        api: list[dict] = [{"role": "system", "content": COMPACT_PROMPT}]
        # Convert to a simple text format for the compaction model call.
        for msg in compaction_messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            if role == "assistant" and msg.get("tool_calls"):
                tc_summary = "; ".join(f"{tc.get('name')}({json.dumps(tc.get('arguments', {}))[:100]})" for tc in msg["tool_calls"])
                content = f"{content} [tools: {tc_summary}]"
            api.append({"role": "user" if role != "assistant" else "assistant", "content": content})

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
        # The recorded usage describes the pre-compaction payload; drop it so
        # the next turn's auto-compact decision uses the fresh context size.
        self.sessions.clear_last_usage(session_id)
        log.info("session %s compressed: %d messages -> %d-char summary", session_id, len(relevant), len(summary))
        return msg

    # -- tool execution -----------------------------------------------------

    def _execute_tool_calls(self, turn_tools: list[dict], turn_content: list[str], api_messages: list[dict], timeline: list[dict], max_tool_chars: int = 8000) -> tuple[list[dict], list[dict]]:
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
            # Truncate the stored result for session persistence.
            truncated_result = _truncate_tool_result(result, max_tool_chars)
            records.append({**t, "result": truncated_result, "status": status})
            log.info("tool call: %s -> %s (%d chars)", t["name"], status, len(result))
            events.append({
                "type": "tool_call",
                "name": t["name"],
                "arguments": t["arguments"],
                "result": result,  # full result to the UI
                "status": status,
            })
            timeline.append({"t": "tool", "name": t["name"], "status": status})
            # Store truncated result in api_messages for the next model call.
            api_messages.append(_tool_result_obj(t["id"], truncated_result))
        return events, records

    # -- main loop ----------------------------------------------------------

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
        max_tool_chars: int = 8000,
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
                    elif ev.kind == "done" and ev.usage:
                        state["last_usage"] = ev.usage
            except ModelError as e:
                error = str(e)
                stop_reason = "model_error"
                log.error("model error in session %s: %s", session_id, e)
                break

            content_parts.extend(turn_content)

            if not turn_tools:
                break

            events, records = self._execute_tool_calls(turn_tools, turn_content, api_messages, timeline, max_tool_chars)
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

        # Automatic compaction: run before building the API message list so
        # the bounded builder can use the fresh summary as a boundary.
        self._maybe_compact(session_id, settings)

        show_thinking = bool(settings.get("show_thinking", True))
        api_messages = self._build_api_messages(session_id, settings)
        backend = self._backend or OpenAICompatBackend(settings["base_url"])
        tools = self.registry.to_openai_tools()
        model = settings["model"]

        # read from settings first, fall back to class default
        max_tool_calls = settings.get("max_tool_calls", self.cfg._DEFAULT_TOOL_ITERATIONS)
        max_tool_chars = int(settings.get("tool_result_max_chars", 8000))

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
            max_tool_chars=max_tool_chars,
        ):
            yield ev

        assistant_msg = self._build_assistant_message(
            state["content_parts"], state["tool_calls"], timeline, state["error"]
        )
        self.sessions.add_message(session_id, assistant_msg)
        if not state["error"] and state.get("last_usage"):
            u = state["last_usage"]
            self.sessions.set_last_usage(
                session_id,
                {
                    "prompt_tokens": u.get("prompt_tokens", 0),
                    "completion_tokens": u.get("completion_tokens", 0),
                    "total_tokens": u.get("total_tokens", 0),
                },
            )
            yield {
                "type": "usage",
                "session_id": session_id,
                "prompt_tokens": u.get("prompt_tokens", 0),
                "completion_tokens": u.get("completion_tokens", 0),
                "total_tokens": u.get("total_tokens", 0),
            }

        if state["error"]:
            yield {"type": "error", "message": state["error"]}
        yield {"type": "done", "stop_reason": state["stop_reason"]}
