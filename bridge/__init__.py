"""OpenAI-compatible API bridge: expose Gremlin to external agents."""

from .server import BridgeError, BridgeServer

__all__ = ["BridgeError", "BridgeServer"]
