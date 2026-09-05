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
def _default_shell_allowlist() -> tuple[str, ...]:
    """Program allow-list for ``run_command`` from GREMLIN_SHELL_ALLOWLIST.

    Empty by default = unrestricted (preserves existing behavior). Set a
    comma-separated list (e.g. ``"ls,cat,git,python"``) to restrict which
    programs the shell tool may execute.
    """
    raw = os.environ.get("GREMLIN_SHELL_ALLOWLIST", "")
    return tuple(p.strip() for p in raw.split(",") if p.strip())


@dataclass
class AppConfig:
    """Runtime configuration. Everything filesystem-related hangs off ``root``."""

    root: Path = field(default_factory=_default_root)

    # --- API bridge ----------------------------------------------------
    bridge_enabled: bool = False
    bridge_host: str = "127.0.0.1"
    bridge_port: int = 8787
    bridge_key: str = ""
    # --- shell safety --------------------------------------------------
    shell_allowlist: tuple[str, ...] = field(default_factory=_default_shell_allowlist)

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
    @property
    def generated_tools_dir(self) -> Path:
        return self.root / "data" / "generated_tools"

    # --- defaults ------------------------------------------------------
    DEFAULT_SETTINGS: ClassVar[dict] = {
        "show_thinking": True,
        "appearance": "dark",  # "light" | "dark"
        "theme": "default",    # bootswatch theme slug or "default"
        "base_url": "http://127.0.0.1:8080/v1",
        "model": os.environ.get("GREMLIN_MODEL", ""),
        "identity": "",  # optional persona text injected into the system prompt
        "discord_enabled": False,  # run the Discord bot (toggle in settings)
        # --- shell safety (opt-in) ----------------------------------------
        "shell_allowlist": [],  # allowed programs for run_command; empty = unrestricted
        "max_tool_calls": 20,  # max tool-loop iterations per user turn
        # --- context management -------------------------------------------
        "max_context_tokens": 32768,      # model context window (hard limit)
        "context_window_turns": 10,       # full turns kept verbatim in the API message list
        "compaction_threshold": 0.65,     # fraction of max_context_tokens that triggers auto-compact
        "tool_result_max_chars": 8000,    # truncate stored tool results beyond this length
        "chars_per_token": 4,           # chars-per-token divisor for context estimation
    }

    _DEFAULT_TOOL_ITERATIONS: ClassVar[int] = 20
    FS_READ_LIMIT: ClassVar[int] = 128 * 1024  # max bytes returned by read_file


def ensure_dirs(cfg: AppConfig) -> None:
    """Create every runtime directory the app may write to.

    Idempotent: safe to call at boot and in tests. Parents and the data
    sub-tree (tasks, generated tools) are all created here.
    """
    dirs = [
        cfg.sessions_dir,
        cfg.data_dir,
        cfg.data_dir / "tasks",
        cfg.generated_tools_dir,
        cfg.skills_dir,
        cfg.transcripts_dir,
    ]
    for p in dirs:
        p.mkdir(parents=True, exist_ok=True)
