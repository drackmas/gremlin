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
import threading
import uuid
from datetime import datetime
from pathlib import Path

from typing import Callable, Iterator

from config import AppConfig
from memory.store import MemoryStore
from models.base import ModelBackend
from models.openai_compat import OpenAICompatBackend
from planning.store import PlanStore
from sessions import SessionManager
from skills.loader import SkillLoader
from tools.registry import SESSION_ID, ToolRegistry
from utils import now_utc

from .compact import Compactor
from .messages import (
    _chars_per_token,
    _estimate_messages_tokens,
    _estimate_tokens,
    _find_last_compression,
    _group_into_turns,
    _stored_to_api,
    _tool_call_obj,
    _tool_result_obj,
    _truncate_tool_result,
)
from .prompts import build_system_prompt

log = logging.getLogger("gremlin.chat")


# ---------------------------------------------------------------------------
# ChatManager
# ---------------------------------------------------------------------------


class ChatManager:
    def __init__(
        self,
        cfg: AppConfig,
        sessions: SessionManager,
        registry: ToolRegistry,
        skills: SkillLoader,
        backend: ModelBackend | None = None,
        memory: MemoryStore | None = None,
    ) -> None:
        self.cfg = cfg
        self.sessions = sessions
        self.registry = registry
        self.skills = skills
        self._backend = backend
        self.memory = memory
        self.plans = PlanStore(Path(cfg.root) / "plan.json")
        # One active generation per session: session_id -> cancel token.
        self._active_runs: dict[str, dict] = {}
        self._active_lock = threading.Lock()
        # Compaction delegate.
        self._compactor = Compactor(sessions, self._get_backend)

    def _get_backend(self, settings: dict) -> ModelBackend:
        """Resolve the model backend for the current request."""
        return self._backend or OpenAICompatBackend(settings["base_url"])

    # -- context building ---------------------------------------------------

    def _build_system(self, settings: dict) -> str:
        """Build the system prompt (always included, never truncated)."""
        return build_system_prompt(
            self.skills.list(),
            tools=self.registry.tools(),
            identity=settings.get("identity", ""),
            now=datetime.now().astimezone().strftime("%A, %B %d, %Y, %H:%M %Z"),
            memory=[m["content"] for m in self.memory.pinned()] if self.memory is not None else None,
            plan=self.plans.summarize(),
        )

    def _build_api_messages(self, session_id: str, settings: dict) -> list[dict]:
        """Build the OpenAI-shaped message list for the model call.

        Bounded by two mechanisms (applied in order):
        1. Turn window: keep at most ``context_window_turns`` full turns.
        2. Token budget: if still over ``max_context_tokens`` (minus system
           prompt and a response reserve), drop oldest messages until it fits.

        A compression summary (if present) replaces everything before it.
        """
        system = self._build_system(settings)
        max_context_tokens = int(settings.get("max_context_tokens", 32768))
        window_turns = int(settings.get("context_window_turns", 10))
        max_tool_chars = int(settings.get("tool_result_max_chars", 8000))

        stored = self.sessions.get(session_id)["messages"]
        cut = _find_last_compression(stored)

        # Messages after the last compression (or all if none).
        tail = stored[cut + 1:] if cut is not None else stored

        # Apply the turn window.
        turns = _group_into_turns(tail)
        if len(turns) > window_turns:
            dropped = len(turns) - window_turns
            log.debug("context: dropping %d oldest turns (window=%d)", dropped, window_turns)
            turns = turns[-window_turns:]

        # Flatten turns back to messages and convert to API format.
        api_messages: list[dict] = [{"role": "system", "content": system}]
        for msg in (m for turn in turns for m in turn):
            api_messages.extend(_stored_to_api(msg, max_tool_chars))

        # Include the compression summary as the first non-system message.
        if cut is not None:
            summary = stored[cut].get("content", "")
            if summary:
                api_messages.insert(1, {"role": "system", "content": f"[Conversation Summary]\n{summary}"})

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
                log.debug(
                    "context: token budget exceeded, dropping %d message(s) (est=%d, budget=%d)",
                    drop_end - 1,
                    msg_tokens,
                    budget,
                )

        return api_messages

    # -- compaction (delegated) ---------------------------------------------

    def _should_compact(self, session_id: str, settings: dict) -> bool:
        """Delegate: check if the session should be compacted."""
        return self._compactor.should_compact(session_id, settings)

    def _maybe_compact(self, session_id: str, settings: dict) -> None:
        """Delegate: trigger automatic compaction if needed."""
        self._compactor.maybe_compact(session_id, settings)

    def compress(self, session_id: str, settings: dict) -> dict:
        """Produce a high-quality summary of the session history.

        Stores the summary as a message with ``compression: True``. The
        ``_build_api_messages`` method uses this as a boundary: everything
        before it is replaced by the summary.
        """
        return self._compactor.compress(session_id, settings)

    # -- tool execution -----------------------------------------------------

    def _execute_tool_calls(
        self,
        turn_tools: list[dict],
        turn_content: list[str],
        api_messages: list[dict],
        timeline: list[dict],
        max_tool_chars: int = 8000,
        is_cancelled: Callable[[], bool] | None = None,
    ) -> tuple[list[dict], list[dict]]:
        """Execute each tool, update api_messages and timeline. Returns (events, tool_call_records).

        If ``is_cancelled`` is set (a newer turn took over the session), no
        further tools from the batch are started. A tool already in flight
        runs to completion -- it cannot be safely killed mid-execution -- and
        its result is discarded along with the rest of the turn.
        """
        # Collapse consecutive duplicate tool calls (same name + arguments) in one
        # turn. Models sometimes repeat an identical call in a loop (e.g. polling a
        # background job's status); executing it once yields the same result as N
        # times, but N identical results flood the context and can push the request
        # past the model's window.
        if len(turn_tools) >= 2:
            deduped: list[dict] = []
            collapsed = 0
            for t in turn_tools:
                sig = (t["name"], json.dumps(t.get("arguments", {}), sort_keys=True))
                if deduped and (deduped[-1]["name"], json.dumps(deduped[-1].get("arguments", {}), sort_keys=True)) == sig:
                    collapsed += 1
                    continue
                deduped.append(t)
            if collapsed:
                log.warning("collapsing %d duplicate tool call(s) in one turn", collapsed)
                turn_tools = deduped

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
            if is_cancelled is not None and is_cancelled():
                # Interrupted mid-batch: skip the remaining tools. The
                # in-flight one (if any) already finished; its result is
                # discarded with the rest of this turn.
                break
            result, ok = self.registry.execute(t["name"], t["arguments"])
            status = "ok" if ok else "error"
            # Truncate the stored result for session persistence.
            truncated_result = _truncate_tool_result(result, max_tool_chars)
            records.append({**t, "result": truncated_result, "status": status})
            log.info("tool call: %s -> %s (%d chars)", t["name"], status, len(result))
            events.append(
                {
                    "type": "tool_call",
                    "name": t["name"],
                    "arguments": t["arguments"],
                    "result": result,  # full result to the UI
                    "status": status,
                }
            )
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
        is_cancelled: Callable[[], bool] | None = None,
    ) -> Iterator[dict]:
        """Stream model turns and execute tool calls for one user turn.

        Yields the user-facing events (``thinking`` / ``text`` /
        ``tool_call``) in order. ``api_messages`` and ``timeline`` are updated
        in place; on exit ``state`` is filled with ``content_parts``,
        ``tool_calls``, ``error`` and ``stop_reason``.

        If ``is_cancelled`` is set (a newer turn took over the session) the
        loop stops at the next check point: the in-flight model stream is
        closed, queued tool calls are not executed, and no further model
        calls are made. The caller (:meth:`run`) discards the partial result
        without saving it.
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

        def _interrupted() -> bool:
            return is_cancelled is not None and is_cancelled()

        limit = max_tool_calls if max_tool_calls is not None else self.cfg._DEFAULT_TOOL_ITERATIONS
        for _ in range(limit):
            if _interrupted():
                stop_reason = "interrupted"
                break
            turn_content: list[str] = []
            turn_thinking: list[str] = []
            turn_tools: list[dict] = []
            stream = None
            try:
                stream = backend.stream(api_messages, tools, model)
                it = iter(stream)
                while True:
                    # Check before pulling the next token: on interrupt the
                    # backend must stop producing tokens immediately, so we
                    # never request the one after a superseded turn.
                    if _interrupted():
                        break
                    try:
                        ev = next(it)
                    except StopIteration:
                        break
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
                    elif ev.kind == "done":
                        if ev.usage:
                            state["last_usage"] = ev.usage
                        break
            except Exception as e:
                if stream is not None:
                    try:
                        stream.close()
                    except Exception:
                        pass
                error = str(e)
                stop_reason = "model_error"
                _append("text", f"(error: {e})")
                content_parts.append(f"(error: {e})")
                log.exception("model stream error in session %s", session_id)
                break
            finally:
                if stream is not None:
                    try:
                        stream.close()
                    except Exception:
                        pass

            # Execute tool calls for this turn (if any).
            if turn_tools:
                events, records = self._execute_tool_calls(
                    turn_tools,
                    turn_content,
                    api_messages,
                    timeline,
                    max_tool_chars=max_tool_chars,
                    is_cancelled=is_cancelled,
                )
                tool_calls.extend(records)
                tc_text = "".join(turn_content)
                if tc_text:
                    content_parts.append(tc_text)
                for ev in events:
                    yield ev
            elif turn_content or turn_thinking:
                # No tool calls: this is a final answer turn.
                content_parts.append("".join(turn_content))
                stop_reason = "completed"
                break
            else:
                # Empty turn: the model produced nothing.
                break

        # If the loop ended without a final answer turn (no error, no
        # interrupt), the iteration limit was exhausted.
        if error is None and stop_reason == "completed" and tool_calls and not content_parts:
            stop_reason = "max_iterations"
            error = f"reached maximum iterations ({limit}) without a final answer"
            _append("text", f"(error: {error})")
            content_parts.append(f"(error: {error})")

        state["content_parts"] = content_parts
        state["tool_calls"] = tool_calls
        state["error"] = error
        state["stop_reason"] = stop_reason

    # -- message building ---------------------------------------------------

    def _build_assistant_message(self, content_parts, tool_calls, timeline, error) -> dict:
        """Assemble the stored assistant message from the run state."""
        content = "".join(p for p in content_parts if p) if content_parts else ""
        msg = {
            "id": uuid.uuid4().hex,
            "role": "assistant",
            "content": content,
            "ts": now_utc(),
        }
        if tool_calls:
            msg["tool_calls"] = tool_calls
        if timeline:
            msg["timeline"] = timeline
        if error:
            msg["error"] = error
        return msg

    # -- run lifecycle ------------------------------------------------------

    def _claim_active(self, session_id: str) -> dict:
        """Register this turn as the active run for *session_id*.

        If a previous turn is still registered it is marked cancelled and
        replaced. Returns the cancel token for this turn.
        """
        token = {"cancelled": False}
        with self._active_lock:
            old = self._active_runs.get(session_id)
            if old is not None:
                old["cancelled"] = True
                log.info("session %s: superseding a previous active run", session_id)
            self._active_runs[session_id] = token
        return token

    def _release_active(self, session_id: str, token: dict) -> None:
        """Drop the registry entry if this turn is still the registered one."""
        with self._active_lock:
            if self._active_runs.get(session_id) is token:
                del self._active_runs[session_id]

    def is_busy(self, session_id: str) -> bool:
        """True when a generation is actively running for *session_id*."""
        with self._active_lock:
            return session_id in self._active_runs

    def abort(self, session_id: str) -> bool:
        """Cooperatively cancel this session's active generation (Stop button).

        Returns True when an active run was cancelled. The interrupted turn
        stops at its next check point (between events / tool iterations), the
        backend stream is closed, and no partial assistant reply is saved.
        The interrupted user message stays in history.
        """
        with self._active_lock:
            token = self._active_runs.get(session_id)
            if token is None:
                return False
            token["cancelled"] = True
            log.info("abort requested for active generation of session %s", session_id)
            return True

    def run(self, session_id: str, user_text: str, settings: dict) -> Iterator[dict]:
        token = self._claim_active(session_id)
        try:
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
            self._compactor.maybe_compact(session_id, settings)

            show_thinking = bool(settings.get("show_thinking", True))
            api_messages = self._build_api_messages(session_id, settings)
            backend = self._get_backend(settings)
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
                is_cancelled=lambda: token["cancelled"],
            ):
                if token["cancelled"]:
                    break  # interrupted: a newer turn owns this session now
                yield ev
            if token["cancelled"]:
                # The interrupted user message stays in the session (the new
                # turn needs it), but no partial assistant reply is saved.
                return

            if not state["error"] and not state["content_parts"] and not state["tool_calls"]:
                # The model completed without producing any answer text or
                # tool calls. Surface it instead of ending the turn with an
                # empty bubble and no error (the user sees "no response").
                state["error"] = "model returned an empty response"
                timeline.append({"t": "text", "text": f"(error: {state['error']})"})
                state["content_parts"].append(f"(error: {state['error']})")
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
        finally:
            token["cancelled"] = True
            self._release_active(session_id, token)
