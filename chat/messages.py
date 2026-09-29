"""Shared message helpers: token estimation, OpenAI shape conversion, turn grouping.

Extracted from ``chat.manager`` so the context-building and compaction
concerns can live in their own modules without circular imports.
"""

from __future__ import annotations

import json

# ---------------------------------------------------------------------------
# Prompts & constants
# ---------------------------------------------------------------------------

COMPACT_PROMPT = (
    "You are a conversation summariser. Produce a dense, self-contained summary "
    "of the dialogue below that a future model can use to continue the task. "
    "Preserve: the user's goals, key decisions, file paths, code snippets that "
    "matter, errors encountered, and the current state of any multi-step work. "
    "Omit: pleasantries, repeated explanations, and tool-call boilerplate. "
    "Write in English. Keep it under 1200 tokens."
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
