"""Discord channel for Gremlin.

    python discord_bot.py     (needs GREMLIN_DISCORD_TOKEN)

The heavy ``discord`` package is imported only inside ``main()`` so the
streaming core below stays importable and testable without it.

Core: ``stream_to_channel(channel, events, chunk_size=1900)`` consumes a
``ChatManager.run()`` event stream, edits a "thinking…" placeholder in
place as text arrives, opens a new message whenever one would exceed
``chunk_size`` characters, and appends a visible error line for
``{"type": "error"}`` events.

The bot keeps a ``{channel_id: session_id}`` map (atomic writes) so a
channel's conversation continues across restarts; ``/new`` in a channel
starts it over. Messages from bots (including self) are ignored.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path

log = logging.getLogger("gremlin.discord")

DISCORD_MAX = 2000
CHUNK_SIZE = DISCORD_MAX - 100  # headroom for error lines
_SENTINEL = object()


async def stream_to_channel(channel, events, chunk_size: int = CHUNK_SIZE, edit_interval: float = 0.25) -> str:
    """Feed a ChatManager.run() event stream into a Discord channel.

    ``channel`` needs async ``send(content)`` and ``edit(message,
    content=...)`` methods returning a message object (discord.py's
    works; so does a fake). Returns the full assistant text (error
    lines included, each on its own line).
    """
    full: list[str] = []
    buf = ""  # text not yet committed to a message
    current = None
    current_content = ""
    last_edit = 0.0

    async def commit(piece: str) -> None:
        nonlocal current, current_content
        if current is None or len(current_content) + len(piece) > chunk_size:
            current = await channel.send(piece)
            current_content = piece
        else:
            current_content += piece
            await channel.edit(current, content=current_content)

    placeholder = await channel.send("thinking…")
    current = placeholder
    current_content = ""  # first edit replaces the placeholder

    for ev in events:
        kind = ev.get("type")
        if kind == "text":
            full.append(ev["text"])
            buf += ev["text"]
            now = time.monotonic()
            while buf and (len(buf) >= chunk_size or now - last_edit >= edit_interval):
                await commit(buf[:chunk_size])
                buf = buf[chunk_size:]
                last_edit = time.monotonic()
        elif kind == "error":
            line = f"⚠ {ev.get('message', 'unknown error')}"
            full.append(line)
            buf = (buf + "\n" if buf else "") + line
            await commit(buf)
            buf = ""
            last_edit = time.monotonic()
        elif kind == "done":
            break

    if buf:
        await commit(buf)
    return "".join(full)


# -- session map (atomic, pattern copied from sessions/manager.py) ----------
def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def _load_map(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def _save_map(path: Path, m: dict) -> None:
    _atomic_write(path, (json.dumps(m, indent=1) + "\n").encode())


# -- the bot -----------------------------------------------------------------
def main() -> int:
    import discord  # heavy; only needed when actually running the bot

    token = os.environ.get("GREMLIN_DISCORD_TOKEN")
    if not token:
        raise SystemExit("GREMLIN_DISCORD_TOKEN is required")

    from config import AppConfig, ensure_dirs
    from chat.manager import ChatManager
    from chat.settings import SettingsStore
    from sessions import SessionManager
    from skills.loader import SkillLoader
    from tools import build_registry

    cfg = AppConfig()
    ensure_dirs(cfg)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    sessions = SessionManager(cfg)
    settings = SettingsStore(cfg)
    s_cfg = settings.load()
    skills = SkillLoader(cfg)
    registry = build_registry(cfg, skills)
    manager = ChatManager(cfg, sessions, registry, skills)

    map_path = cfg.data_dir / "discord_sessions.json"
    session_map = _load_map(map_path)

    intents = discord.Intents.default()
    intents.message_content = True
    client = discord.Client(intents=intents)

    def produce(sid: str, text: str, queue: asyncio.Queue, loop: asyncio.AbstractEventLoop) -> None:
        """Run the (synchronous) model turn on a worker thread, forwarding
        events to the running loop so the Discord client never blocks."""
        try:
            for ev in manager.run(sid, text, s_cfg):
                loop.call_soon_threadsafe(queue.put_nowait, ev)
        finally:
            loop.call_soon_threadsafe(queue.put_nowait, _SENTINEL)

    async def on_message(message) -> None:
        if message.author == client.user or message.author.bot:
            return
        text = message.content.strip()
        if not text or message.channel is None:
            return

        key = str(message.channel.id)
        if text.startswith("/new"):
            session_map[key] = sessions.create("discord")
            _save_map(map_path, session_map)
            await message.channel.send(f"new session {session_map[key][:8]}")
            return

        sid = session_map.get(key)
        if sid is None:
            sid = session_map[key] = sessions.create("discord")
            _save_map(map_path, session_map)

        queue: asyncio.Queue = asyncio.Queue()
        loop = asyncio.get_running_loop()
        loop.run_in_executor(None, produce, sid, text, queue, loop)

        async def events():
            while True:
                ev = await queue.get()
                if ev is _SENTINEL:
                    return
                yield ev

        await stream_to_channel(message.channel, events())

    client.add_listener(on_message, "on_message")
    client.run(token)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
