"""Memory tools: the model-facing access to the persistent memory store."""

from __future__ import annotations

import json

from memory.store import MemoryStore

from .registry import Tool


def build_memory_tools(store: MemoryStore) -> list[Tool]:
    def _brief(m: dict) -> dict:
        return {
            "id": m["id"],
            "content": m["content"],
            "tags": m.get("tags", []),
            "pinned": m.get("pinned", False),
        }

    def memory_add(args: dict) -> str:
        memory_id = store.add(args["content"], tags=args.get("tags"), pin=bool(args.get("pin")))
        return f"stored memory {memory_id}"

    def memory_search(args: dict) -> str:
        results = store.search(args["query"], in_content=bool(args.get("in_content", True)))
        if not results:
            return "no matching memories"
        return json.dumps([_brief(m) for m in results], ensure_ascii=False)

    def memory_get(args: dict) -> str:
        m = store.get(args["id"])
        if m is None:
            return f"no memory with id {args['id']}"
        return json.dumps(_brief(m), ensure_ascii=False)

    def memory_remove(args: dict) -> str:
        return (
            f"removed memory {args['id']}"
            if store.remove(args["id"])
            else f"no memory with id {args['id']}"
        )

    def memory_pin(args: dict) -> str:
        value = bool(args.get("value", True))
        if not store.pin(args["id"], value=value):
            return f"no memory with id {args['id']}"
        return f"memory {args['id']} pinned={value}"

    return [
        Tool(
            name="memory_add",
            description="Store a durable fact, decision, or preference in long-term memory. Returns the memory id.",
            parameters={
                "type": "object",
                "properties": {
                    "content": {"type": "string", "description": "the fact to remember"},
                    "tags": {"type": "array", "description": "optional short tags"},
                    "pin": {"type": "boolean", "description": "pin into the system prompt every turn"},
                },
                "required": ["content"],
            },
            handler=memory_add,
        ),
        Tool(
            name="memory_search",
            description="Search long-term memory by query (matches content and tags).",
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "search text"},
                    "in_content": {"type": "boolean", "description": "also match inside content (default true)"},
                },
                "required": ["query"],
            },
            handler=memory_search,
        ),
        Tool(
            name="memory_get",
            description="Fetch one memory by its id.",
            parameters={
                "type": "object",
                "properties": {"id": {"type": "string", "description": "memory id"}},
                "required": ["id"],
            },
            handler=memory_get,
        ),
        Tool(
            name="memory_remove",
            description="Delete a memory by its id.",
            parameters={
                "type": "object",
                "properties": {"id": {"type": "string", "description": "memory id"}},
                "required": ["id"],
            },
            handler=memory_remove,
        ),
        Tool(
            name="memory_pin",
            description="Pin or unpin a memory (pinned memories appear in the system prompt every turn).",
            parameters={
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "memory id"},
                    "value": {"type": "boolean", "description": "true to pin, false to unpin (default true)"},
                },
                "required": ["id"],
            },
            handler=memory_pin,
        ),
    ]
