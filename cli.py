"""Terminal REPL for Gremlin.

    python cli.py

Talks to the same ChatManager the web UI uses: same sessions, same
memory, same tools. Commands: /new, /help, /quit. Ctrl+C cancels the
answer in progress (best effort: the in-flight model request is left to
finish server-side), Ctrl+D or /quit exits.
"""

from __future__ import annotations

import logging
import os
import sys
import time

from config import AppConfig, ensure_dirs
from chat.manager import ChatManager
from chat.settings import SettingsStore
from sessions import SessionManager
from skills.loader import SkillLoader
from memory.store import MemoryStore
from tools import build_registry

HELP = """commands:
  /new    start a fresh session
  /help   show this help
  /quit   exit (Ctrl+D works too)
Ctrl+C cancels the answer in progress."""


class _Paint:
    """Tiny ANSI helper; disabled for pipes or when NO_COLOR is set."""

    def __init__(self, enabled: bool) -> None:
        self.on = enabled

    def paint(self, text: str, code: str) -> str:
        return f"\x1b[{code}m{text}\x1b[0m" if self.on else text

    def prompt(self, text: str) -> str:
        return self.paint(text, "1;36")

    def dim(self, text: str) -> str:
        return self.paint(text, "2")

    def err(self, text: str) -> str:
        return self.paint(text, "31")


def _handle_command(text: str, sessions: SessionManager) -> tuple[str, str | None]:
    """Parse a /command. Returns (action, detail); action is one of
    'quit' | 'new' | 'help' | 'unknown'."""
    cmd = text.split()
    if cmd[0] in ("/quit", "/exit"):
        return "quit", None
    if cmd[0] == "/new":
        s = sessions.create("terminal")
        return "new", s["id"]
    if cmd[0] == "/help":
        return "help", HELP
    return "unknown", cmd[0]


def main(cfg=None, backend=None) -> int:
    cfg = cfg or AppConfig()
    ensure_dirs(cfg)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    for noisy in ("werkzeug", "httpx", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    sessions = SessionManager(cfg)
    settings = SettingsStore(cfg)
    s_cfg = settings.load()
    skills = SkillLoader(cfg)
    memory = MemoryStore(cfg.data_dir / "memory.json")
    registry = build_registry(cfg, skills, memory)
    manager = ChatManager(cfg, sessions, registry, skills, backend=backend, memory=memory)
    s = sessions.create("terminal")

    p = _Paint(sys.stdout.isatty() and not os.environ.get("NO_COLOR"))
    print(p.dim(f"gremlin · {s_cfg.get('model')} · session {s['id'][:8]}"))
    print(p.dim("commands: /new /help /quit · Ctrl+C cancels · Ctrl+D exits"))

    while True:
        try:
            text = input(p.prompt("you> ")).strip()
        except EOFError:
            print()
            break
        except KeyboardInterrupt:
            continue  # bare Ctrl+C at the prompt: just re-prompt
        if not text:
            continue

        if text.startswith("/"):
            action, detail = _handle_command(text, sessions)
            if action == "quit":
                break
            if action == "new":
                s = sessions.create("terminal")
                print(p.dim(f"new session {s['id'][:8]}"))
            elif action == "help":
                print(detail)
            else:
                print(p.err(f"unknown command {detail} — /help"))
            continue

        t0 = time.monotonic()
        n = 0
        cancelled = False
        try:
            for ev in manager.run(s["id"], text, s_cfg):
                if ev["type"] == "text":
                    n += len(ev["text"])
                    print(ev["text"], end="", flush=True)
                elif ev["type"] == "error":
                    print(p.err(f"\nerror: {ev['message']}"))
                elif ev["type"] == "done":
                    break
        except KeyboardInterrupt:
            cancelled = True
        print()
        if cancelled:
            print(p.dim("· cancelled"))
        else:
            print(p.dim(f"· {n} chars · {time.monotonic() - t0:.1f}s"))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
