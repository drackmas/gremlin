"""Discord channel: stream_to_channel with a fake channel + session map."""

from __future__ import annotations

import asyncio

import discord_bot


class FakeMessage:
    def __init__(self, n, channel):
        self.n = n
        self.content = ""
        self._channel = channel

    async def edit(self, content=None):
        self._channel.edits += 1
        self.content = content

class FakeChannel:
    def __init__(self):
        self.messages = []
        self.edits = 0
        self.sends = 0

    async def send(self, content):
        self.sends += 1
        m = FakeMessage(len(self.messages) + 1, self)
        m.content = content
        self.messages.append(m)
        return m



def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)
async def aseq(seq):
    for ev in seq:
        yield ev


def test_short_reply_edits_placeholder():
    ch = FakeChannel()
    events = [{"type": "text", "text": "hello "}, {"type": "text", "text": "there"}, {"type": "done"}]
    out = run(discord_bot.stream_to_channel(ch, aseq(events), edit_interval=0))
    assert out == "hello there"
    assert ch.sends == 1  # only the placeholder
    assert ch.messages[0].content == "hello there"
    assert ch.edits == 2  # one per text event (interval=0)


def test_long_reply_splits_messages():
    ch = FakeChannel()
    text = "x" * 3801  # > 2 * chunk_size
    events = [{"type": "text", "text": text}, {"type": "done"}]
    out = run(discord_bot.stream_to_channel(ch, aseq(events), edit_interval=0))
    assert out == text
    # placeholder (edited) + 2 new messages
    assert len(ch.messages) == 3
    assert all(len(m.content) <= discord_bot.CHUNK_SIZE for m in ch.messages)
    # concatenation of the final message contents matches the original text
    assert "".join(m.content for m in ch.messages) == text
    # final content of each message is the committed piece
    assert ch.messages[0].content == text[: discord_bot.CHUNK_SIZE]
    assert ch.messages[1].content == text[discord_bot.CHUNK_SIZE : 2 * discord_bot.CHUNK_SIZE]
    assert ch.messages[2].content == text[2 * discord_bot.CHUNK_SIZE :]


def test_error_event_visible():
    ch = FakeChannel()
    events = [
        {"type": "text", "text": "ok "},
        {"type": "error", "message": "boom"},
        {"type": "done"},
    ]
    out = run(discord_bot.stream_to_channel(ch, aseq(events), edit_interval=0))
    assert "ok" in out and "⚠ boom" in out
    assert "⚠ boom" in ch.messages[0].content


def test_empty_stream_leaves_placeholder():
    ch = FakeChannel()
    out = run(discord_bot.stream_to_channel(ch, aseq([{"type": "done"}]), edit_interval=0))
    assert out == ""
    assert ch.messages[0].content == "thinking…"


def test_session_map_roundtrip(tmp_path):
    p = tmp_path / "map.json"
    assert discord_bot._load_map(p) == {}  # missing file
    discord_bot._save_map(p, {"123": "abc"})
    assert discord_bot._load_map(p) == {"123": "abc"}
    # corrupt file -> empty, no crash
    p.write_text("{not json")
    assert discord_bot._load_map(p) == {}
    # no tmp file left behind
    assert list(tmp_path.iterdir()) == [p]
