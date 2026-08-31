"""OpenAI-compatible chat-completions backend (works with llama.cpp, vLLM, etc.).

Streams server-sent events and normalizes them into ``ModelEvent`` objects.
Provider-specific fields (``reasoning_content`` for thinking, accumulated
``tool_calls`` fragments) are absorbed here so callers only see normalized
events.
"""

from __future__ import annotations

import json
import logging
from typing import Iterator

import requests

from .base import ModelBackend, ModelEvent, ModelError

log = logging.getLogger("gremlin.model")

_CONNECT_TIMEOUT = 10
_READ_TIMEOUT = 600  # local models can be slow to first token


class OpenAICompatBackend(ModelBackend):
    def __init__(self, base_url: str) -> None:
        # Accept with or without trailing /v1; we append /chat/completions.
        self.base_url = (base_url or "").rstrip("/")
        if not self.base_url:
            raise ModelError("base_url is required")

    def _url(self) -> str:
        return self.base_url if self.base_url.endswith("/chat/completions") else self.base_url + "/chat/completions"

    def stream(self, messages, tools, model) -> Iterator[ModelEvent]:
        payload = {"model": model, "messages": messages, "stream": True}
        if tools:
            payload["tools"] = tools
        try:
            resp = requests.post(
                self._url(),
                json=payload,
                headers={"Content-Type": "application/json"},
                stream=True,
                timeout=(_CONNECT_TIMEOUT, _READ_TIMEOUT),
            )
        except requests.RequestException as e:
            log.error("model connection failed: %s", e)
            raise ModelError(f"cannot reach model at {self.base_url}: {e}") from e

        if resp.status_code != 200:
            detail = resp.text[:500]
            log.error("model HTTP %s: %s", resp.status_code, detail)
            raise ModelError(f"model returned HTTP {resp.status_code}", resp.status_code, detail)

        # Accumulate tool-call fragments per index until the stream ends.
        tool_fragments: dict[int, dict] = {}

        def flush_tools() -> list[ModelEvent]:
            events: list[ModelEvent] = []
            for idx in sorted(tool_fragments):
                frag = tool_fragments[idx]
                args_raw = frag.get("arguments", "")
                try:
                    args = json.loads(args_raw) if args_raw else {}
                    if not isinstance(args, dict):
                        args = {"value": args}
                except json.JSONDecodeError:
                    args = {"_raw": args_raw}
                events.append(
                    ModelEvent(
                        kind="tool_call",
                        tool_call_id=frag.get("id") or f"call_{idx}",
                        name=frag.get("name", ""),
                        arguments=args,
                    )
                )
            tool_fragments.clear()
            return events

        try:
            for raw_line in resp.iter_lines(decode_unicode=True):
                if raw_line is None:
                    continue
                # Defensive: a single line could (rarely) hold multiple events.
                for line in raw_line.split("\n"):
                    line = line.strip()
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[len("data:"):].strip()
                    if data == "[DONE]":
                        for ev in flush_tools():
                            yield ev
                        yield ModelEvent(kind="done", finish_reason="stop")
                        return
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    choices = chunk.get("choices") or []
                    if not choices:
                        continue
                    choice = choices[0]
                    delta = choice.get("delta") or {}

                    if delta.get("reasoning_content"):
                        yield ModelEvent(kind="thinking", text=delta["reasoning_content"])
                    if delta.get("content"):
                        yield ModelEvent(kind="text", text=delta["content"])

                    for tc in delta.get("tool_calls") or []:
                        idx = tc.get("index", 0)
                        frag = tool_fragments.setdefault(idx, {"id": "", "name": "", "arguments": ""})
                        if tc.get("id"):
                            frag["id"] = tc["id"]
                        fn = tc.get("function") or {}
                        if fn.get("name"):
                            frag["name"] = frag.get("name", "") + fn["name"]
                        if fn.get("arguments"):
                            frag["arguments"] += fn["arguments"]

                    finish = choice.get("finish_reason")
                    if finish:
                        for ev in flush_tools():
                            yield ev
                        yield ModelEvent(kind="done", finish_reason=finish)
                        return
        finally:
            resp.close()

        # Stream ended without an explicit finish_reason.
        for ev in flush_tools():
            yield ev
        yield ModelEvent(kind="done", finish_reason="stop")
