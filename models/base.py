"""Abstract model backend and normalized event types."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterator


class ModelError(Exception):
    """Raised when the model backend fails (network, HTTP, bad stream)."""

    def __init__(self, message: str, status: int | None = None, detail: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.detail = detail


@dataclass
class ModelEvent:
    """One normalized model event.

    kind:
        "text"       -- assistant text delta (``text``)
        "thinking"   -- reasoning delta, if the backend exposes it (``text``)
        "tool_call"  -- a complete tool call (``tool_call_id``, ``name``, ``arguments``)
        "done"       -- stream finished (``finish_reason``); carries the
                       provider ``usage`` dict when the backend reports it
    """

    kind: str
    text: str = ""
    tool_call_id: str = ""
    name: str = ""
    arguments: dict[str, Any] = field(default_factory=dict)
    finish_reason: str = ""
    usage: dict[str, Any] = field(default_factory=dict)


class ModelBackend:
    """Interface every model provider adapter must implement."""

    def stream(
        self,
        messages: list[dict],
        tools: list[dict],
        model: str,
    ) -> Iterator[ModelEvent]:
        raise NotImplementedError
