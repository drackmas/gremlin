"""Persistent application settings (data/settings.json).

Settings saved from the settings page land on disk here and are reloaded on
every app start and every page load, so they survive reloads and restarts.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile

log = logging.getLogger("gremlin.settings")

_APPEARANCES = ("light", "dark")


class SettingsError(ValueError):
    """Raised when submitted settings fail validation."""


def _atomic_write(path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class SettingsStore:
    def __init__(self, cfg) -> None:
        self.cfg = cfg

    def load(self) -> dict:
        defaults = dict(self.cfg.DEFAULT_SETTINGS)
        try:
            with open(self.cfg.settings_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                raise ValueError("settings file is not an object")
        except FileNotFoundError:
            return defaults
        except (json.JSONDecodeError, OSError, ValueError) as e:
            log.warning("settings file unreadable (%s); using defaults", e)
            return defaults
        merged = {**defaults, **{k: v for k, v in data.items() if k in defaults}}
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
                if not isinstance(value, str) or not value.strip():
                    raise SettingsError(f"{key} must be a non-empty string")
                value = value.strip()
                if key == "appearance" and value not in _APPEARANCES:
                    raise SettingsError("appearance must be 'light' or 'dark'")
            else:
                continue
            current[key] = value
        _atomic_write(self.cfg.settings_path, current)
        log.info("settings saved: %s", {k: current[k] for k in current if k != "model"})
        return current
