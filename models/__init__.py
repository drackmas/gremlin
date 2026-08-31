"""Model backends.

``ModelBackend`` normalizes provider differences into a small set of events
so the rest of the app (and the browser) never sees provider-specific shapes.
"""

from .base import ModelBackend, ModelEvent, ModelError
from .openai_compat import OpenAICompatBackend

__all__ = ["ModelBackend", "ModelEvent", "ModelError", "OpenAICompatBackend"]
