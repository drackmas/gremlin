import asyncio
import json
import logging
import os
import time
import threading
from pathlib import Path

from utils import atomic_write_json

log = logging.getLogger("gremlin.discord")

OWNER_ID = int(os.environ.get("GREMLIN_DISCORD_OWNER_ID", "0"))  # your Discord user ID
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
            await current.edit(content=current_content)

    placeholder = await channel.send("thinking…")
    current = placeholder
    current_content = ""  # first edit replaces the placeholder

    async for ev in events:
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


# -- session map -------------------------------------------------------------
def _load_map(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def _save_map(path: Path, m: dict) -> None:
    atomic_write_json(path, m)


# -- client factory -----------------------------------------------------------
def _make_client(manager, sessions, s_cfg, map_path: Path, session_map: dict):
    """Build the discord.Client with Gremlin's message handler attached.

    ``discord`` is imported here so the heavy dependency is only pulled in when
    a bot is actually being started. ``s_cfg`` is the settings dict captured at
    start time.
    """
    import discord

    intents = discord.Intents.default()
    intents.message_content = True
    client = discord.Client(intents=intents)
    client._gremlin_loop = None  # type: ignore[attr-defined]

    async def on_ready() -> None:
        client._gremlin_loop = asyncio.get_running_loop()  # type: ignore[attr-defined]
        assert client.user is not None
        log.info("discord logged in as %s (id=%s)", client.user, client.user.id)

    def produce(sid: str, text: str, queue: asyncio.Queue, loop) -> None:
        """Run the synchronous model turn on a worker thread, forwarding events
        to the running loop so the Discord client never blocks."""
        try:
            for ev in manager.run(sid, text, s_cfg):
                loop.call_soon_threadsafe(queue.put_nowait, ev)
        finally:
            loop.call_soon_threadsafe(queue.put_nowait, _SENTINEL)

    async def on_message(message) -> None:
        assert client.user is not None
        # Ignore messages sent by Gremlin herself or by any other Discord bot.
        if message.author == client.user or message.author.bot:
            return

        # Only the bot owner can trigger Gremlin.
        if message.author.id != OWNER_ID:
            return

        # Gremlin must be explicitly mentioned.
        if client.user not in message.mentions:
            return

        # Remove the @Gremlin mention before sending the message to the AI.
        text = message.content
        text = text.replace(f"<@{client.user.id}>", "")
        text = text.replace(f"<@!{client.user.id}>", "")
        text = text.strip()

        if not text or message.channel is None:
            return
        key = str(message.channel.id)
        if text.startswith("/new"):
            session_map[key] = sessions.create("discord")["id"]
            _save_map(map_path, session_map)
            await message.channel.send(f"new session {session_map[key][:8]}")
            return
        sid = session_map.get(key)
        if sid is None:
            sid = session_map[key] = sessions.create("discord")["id"]
            _save_map(map_path, session_map)
        queue: asyncio.Queue = asyncio.Queue()
        loop = asyncio.get_running_loop()
        loop.run_in_executor(None, produce, sid, text, queue, loop)  # type: ignore[arg-type]

        async def events():
            while True:
                ev = await queue.get()
                if ev is _SENTINEL:
                    return
                yield ev

        await stream_to_channel(message.channel, events())

    client.event(on_ready)
    client.event(on_message)
    return client


class DiscordBot:
    """Start/stop wrapper around the Discord client for runtime control.

    The token is read from the ``GREMLIN_DISCORD_TOKEN`` env var (populated from
    ``.env``). The client runs on its own daemon thread with its own event loop
    so it can coexist with the Flask app.
    """

    def __init__(self, manager, sessions, load_settings, map_path: Path) -> None:
        self.manager = manager
        self.sessions = sessions
        self.load_settings = load_settings
        self.map_path = map_path
        self.session_map = _load_map(map_path)
        self._client = None
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return self._client is not None

    def start(self) -> None:
        """Connect and run the bot on a background thread (no-op if running)."""
        if self._client is not None:
            return
        token = os.environ.get("GREMLIN_DISCORD_TOKEN")
        if not token:
            raise ValueError("GREMLIN_DISCORD_TOKEN is not set")
        client = _make_client(self.manager, self.sessions, self.load_settings(), self.map_path, self.session_map)
        self._client = client
        self._thread = threading.Thread(
            target=client.run,
            args=(token,),
            kwargs={"log_handler": None},
            daemon=True,
            name="gremlin-discord",
        )
        self._thread.start()
        log.info("discord bot starting")

    def stop(self) -> None:
        """Disconnect the bot (no-op if not running)."""
        client, self._client = self._client, None
        self._thread = None
        if client is None:
            return
        loop = getattr(client, "_gremlin_loop", None)
        if loop is not None and loop.is_running():
            asyncio.run_coroutine_threadsafe(client.close(), loop)
        log.info("discord bot stopping")


# -- the bot -----------------------------------------------------------------
def main() -> int:
    token = os.environ.get("GREMLIN_DISCORD_TOKEN")
    if not token:
        raise SystemExit("GREMLIN_DISCORD_TOKEN is required")

    from config import AppConfig, ensure_dirs
    from chat.manager import ChatManager
    from chat.settings import SettingsStore
    from sessions import SessionManager
    from memory.store import MemoryStore
    from skills.loader import SkillLoader
    from tools import build_registry

    cfg = AppConfig()
    ensure_dirs(cfg)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    sessions = SessionManager(cfg)
    settings = SettingsStore(cfg)
    s_cfg = settings.load()
    skills = SkillLoader(cfg)
    memory = MemoryStore(cfg.data_dir / "memory.json")
    registry = build_registry(cfg, skills, memory_store=memory)
    manager = ChatManager(cfg, sessions, registry, skills, memory=memory)

    map_path = cfg.data_dir / "discord_sessions.json"
    session_map = _load_map(map_path)

    client = _make_client(manager, sessions, s_cfg, map_path, session_map)
    client.run(token)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
