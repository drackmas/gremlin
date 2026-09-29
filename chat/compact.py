"""Context compaction: summarises long sessions to stay within the model window.

Extracted from ``ChatManager`` so the compaction logic is independently
testable and the manager stays focused on the run/tool loop.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Callable

from models.base import ModelBackend, ModelError
from sessions.manager import SessionManager
from utils import now_utc

from .messages import (
    COMPACT_PROMPT,
    _chars_per_token,
    _estimate_messages_tokens,
    _find_last_compression,
)

log = logging.getLogger("gremlin.chat.compact")


class Compactor:
    """Produces and applies session compression summaries.

    *sessions* is the backing store. *backend_for* is a callable that
    receives the settings dict and returns a ``ModelBackend`` to use for
    the compaction LLM call.
    """

    def __init__(self, sessions: SessionManager, backend_for: Callable[[dict], ModelBackend]) -> None:
        self._sessions = sessions
        self._backend_for = backend_for

    # -- decision -----------------------------------------------------------

    def should_compact(self, session_id: str, settings: dict) -> bool:
        """Check if the session's context is close enough to the window to compact.

        Prefers the real prompt-token usage from the last model request
        (recorded on the session); falls back to a chars/4 estimate of the
        stored messages when the provider reported nothing.
        """
        max_context_tokens = int(settings.get("max_context_tokens", 32768))
        threshold = float(settings.get("compaction_threshold", 0.65))
        budget = int(max_context_tokens * threshold)

        session = self._sessions.get(session_id)
        stored = session["messages"]
        cut = _find_last_compression(stored)
        # Only consider messages after the last compression.
        relevant = stored[cut + 1:] if cut is not None else stored

        # Real usage from the last model request, when the provider reported it.
        usage = session.get("last_usage") or {}
        last_prompt = usage.get("prompt_tokens")
        if isinstance(last_prompt, int) and last_prompt >= budget:
            return True
        est = _estimate_messages_tokens(
            [
                {"role": m.get("role"), "content": m.get("content", ""), "tool_calls": m.get("tool_calls")}
                for m in relevant
            ],
            _chars_per_token(settings),
        )
        return est > budget

    def maybe_compact(self, session_id: str, settings: dict) -> None:
        """Trigger automatic compaction if the context is too large.

        Runs the model to produce a summary, stores it as a compression marker,
        and the next _build_api_messages will use it to bound history.
        """
        if not self.should_compact(session_id, settings):
            return

        log.info("session %s: auto-compaction triggered", session_id)
        try:
            self.compress(session_id, settings)
            log.info("session %s: auto-compaction complete", session_id)
        except Exception as e:
            # Compaction failure should not break the turn; log and continue.
            log.warning(
                "session %s: auto-compaction failed (%s); continuing with full context",
                session_id,
                e,
            )

    # -- execution ----------------------------------------------------------

    def compress(self, session_id: str, settings: dict) -> dict:
        """Produce a high-quality summary of the session history.

        Stores the summary as a message with ``compression: True``. The
        ``_build_api_messages`` method uses this as a boundary: everything
        before it is replaced by the summary.
        """
        stored = self._sessions.get(session_id)["messages"]
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
                tc_summary = "; ".join(
                    f"{tc.get('name')}({json.dumps(tc.get('arguments', {}))[:100]})"
                    for tc in msg["tool_calls"]
                )
                content = f"{content} [tools: {tc_summary}]"
            api.append({"role": "user" if role != "assistant" else "assistant", "content": content})

        backend = self._backend_for(settings)
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
        self._sessions.add_message(session_id, msg)
        # The recorded usage describes the pre-compaction payload; drop it so
        # the next turn's auto-compact decision uses the fresh context size.
        self._sessions.clear_last_usage(session_id)
        log.info("session %s compressed: %d messages -> %d-char summary", session_id, len(relevant), len(summary))
        return msg
