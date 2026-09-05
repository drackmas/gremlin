"""Persistent application settings (data/settings.json).

Settings saved from the settings page land on disk here and are reloaded on
every app start and every page load, so they survive reloads and restarts.
"""

from __future__ import annotations

import json
import logging

from dotenv import dotenv_values, set_key

from utils import atomic_write_json

log = logging.getLogger("gremlin.settings")

_APPEARANCES = ("light", "dark")


class SettingsError(ValueError):
    """Raised when submitted settings fail validation."""



class SettingsStore:
    def __init__(self, cfg) -> None:
        self.cfg = cfg

    def load(self) -> dict:
        defaults = dict(self.cfg.DEFAULT_SETTINGS)
        data: dict = {}
        try:
            with open(self.cfg.settings_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                raise ValueError("settings file is not an object")
        except FileNotFoundError:
            pass
        except (json.JSONDecodeError, OSError, ValueError) as e:
            log.warning("settings file unreadable (%s); using defaults", e)
            data = {}
        merged = {**defaults, **{k: v for k, v in data.items() if k in defaults}}
        merged["discord_token_set"] = bool(self._env_token())
        return merged

    def save(self, data: dict) -> dict:
        if not isinstance(data, dict):
            raise SettingsError("settings must be an object")
        current = self.load()
        for key, default in self.cfg.DEFAULT_SETTINGS.items():
            if key not in data:
                continue
            value = data[key]
            if isinstance(default, bool):
                if not isinstance(value, bool):
                    raise SettingsError(f"{key} must be a boolean")
            elif isinstance(default, str):
                if not isinstance(value, str):
                    raise SettingsError(f"{key} must be a string")
                value = value.strip()
                if not value and key != "identity":
                    raise SettingsError(f"{key} must be a non-empty string")
                if key == "appearance" and value not in _APPEARANCES:
                    raise SettingsError("appearance must be 'light' or 'dark'")
            elif isinstance(default, int):
                if not isinstance(value, int) or isinstance(value, bool):
                    raise SettingsError(f"{key} must be an integer")
                if value < 1:
                    raise SettingsError(f"{key} must be a positive integer")
            elif isinstance(default, float):
                # Fractions like compaction_threshold: accept int/float in (0, 1].
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    raise SettingsError(f"{key} must be a number between 0 and 1")
                if not (0 < value <= 1):
                    raise SettingsError(f"{key} must be a fraction in (0, 1]")
            elif isinstance(default, list):
                # shell_allowlist: accept a list of strings or a comma string.
                if isinstance(value, str):
                    value = value.split(",")
                elif isinstance(value, list):
                    if not all(isinstance(p, str) for p in value):
                        raise SettingsError(f"{key} must be a list of strings")
                else:
                    raise SettingsError(f"{key} must be a list of program names")
                value = [p.strip() for p in value if str(p).strip()]
            else:
                continue
            current[key] = value
        atomic_write_json(self.cfg.settings_path, current)
        token = data.get("GREMLIN_DISCORD_TOKEN")
        if isinstance(token, str) and token.strip():
            self._write_env_token(token.strip())
            log.info("GREMLIN_DISCORD_TOKEN written to %s", self.cfg.env_path)
        log.info("settings saved: %s", {k: current[k] for k in current if k != "model"})
        return current

    def _env_token(self) -> str:
        """Current GREMLIN_DISCORD_TOKEN from the .env file ('' if absent)."""
        values = dotenv_values(self.cfg.env_path)
        return (values.get("GREMLIN_DISCORD_TOKEN") or "").strip()

    def _write_env_token(self, token: str) -> None:
        """Create/update the .env file so the Discord token persists to disk."""
        env_path = self.cfg.env_path
        env_path.parent.mkdir(parents=True, exist_ok=True)
        if not env_path.exists():
            env_path.write_text("", encoding="utf-8")
        set_key(str(env_path), "GREMLIN_DISCORD_TOKEN", token)
