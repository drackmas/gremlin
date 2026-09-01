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
        "model": "/home/dracmas/Downloads/Qwen3.8-27B-UD-Q8_K_XL.gguf",
        "identity": "",  # optional persona text injected into the system prompt
    }

    MAX_TOOL_ITERATIONS: ClassVar[int] = 8
    FS_READ_LIMIT: ClassVar[int] = 128 * 1024  # max bytes returned by read_file


def ensure_dirs(cfg: AppConfig) -> None:
    """Create runtime directories that may not exist yet."""
    for p in (cfg.sessions_dir, cfg.data_dir):
        p.mkdir(parents=True, exist_ok=True)
