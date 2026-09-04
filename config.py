"""Application configuration.

All paths derive from a single project root so tests can point the app at a
temporary tree (see ``AppConfig``).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar


def _default_root() -> Path:
    return Path(os.environ.get("GREMLIN_ROOT", Path(__file__).resolve().parent))


@dataclass
class AppConfig:
    """Runtime configuration. Everything filesystem-related hangs off ``root``."""

    root: Path = field(default_factory=_default_root)

    # --- API bridge ----------------------------------------------------
    bridge_enabled: bool = False
    bridge_host: str = "127.0.0.1"
    bridge_port: int = 8787
    bridge_key: str = ""

    # --- derived paths -------------------------------------------------
    @property
    def sessions_dir(self) -> Path:
        return self.root / "sessions"

    @property
    def skills_dir(self) -> Path:
        return self.root / "skills"

    @property
    def data_dir(self) -> Path:
        return self.root / "data"

    @property
    def settings_path(self) -> Path:
        return self.data_dir / "settings.json"

    @property
    def env_path(self) -> Path:
        return self.root / ".env"

    @property
    def frontend_dir(self) -> Path:
        return self.root / "frontend"

    @property
    def templates_dir(self) -> Path:
        return self.frontend_dir / "templates"

    @property
    def static_dir(self) -> Path:
        return self.frontend_dir / "static"

    @property
    def themes_dir(self) -> Path:
        return self.static_dir / "themes"

    @property
    def transcripts_dir(self) -> Path:
        return self.root / "files" / "transcripts"

    # --- defaults ------------------------------------------------------
    DEFAULT_SETTINGS: ClassVar[dict] = {
        "show_thinking": True,
        "appearance": "dark",  # "light" | "dark"
        "theme": "default",    # bootswatch theme slug or "default"
        "base_url": "http://127.0.0.1:8080/v1",
        "model": os.environ.get("GREMLIN_MODEL", ""),
        "identity": "",  # optional persona text injected into the system prompt
        "discord_enabled": False,  # run the Discord bot (toggle in settings)
        "max_tool_calls": 20,  # max tool-loop iterations per user turn
        # --- context management -------------------------------------------
        "max_context_tokens": 32768,      # model context window (hard limit)
        "context_window_turns": 10,       # full turns kept verbatim in the API message list
        "compaction_threshold": 0.65,     # fraction of max_context_tokens that triggers auto-compact
        "tool_result_max_chars": 8000,    # truncate stored tool results beyond this length
    }

    _DEFAULT_TOOL_ITERATIONS: ClassVar[int] = 20
    FS_READ_LIMIT: ClassVar[int] = 128 * 1024  # max bytes returned by read_file


def ensure_dirs(cfg: AppConfig) -> None:
    """Create runtime directories that may not exist yet."""
    for p in (cfg.sessions_dir, cfg.data_dir):
        p.mkdir(parents=True, exist_ok=True)
