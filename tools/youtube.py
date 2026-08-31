"""YouTube tools backed by the ``yt_dlp`` Python package (not the CLI).

Two tools are exposed to the model:

* ``youtube_transcript(url, lang)`` -- fetch a video's transcript. Manual
  subtitles are preferred; auto-generated captions are the fallback.
* ``youtube_video_info(url)`` -- title, duration, uploader, description.

Failures raise :class:`YoutubeError`; the registry converts that into a
structured ``ERROR:`` string so the model can react (e.g. try another lang).
"""

import logging
import re
import tempfile
from pathlib import Path

import requests

from .registry import Tool

log = logging.getLogger("gremlin.tools.youtube")

UA = "Mozilla/5.0 (X11; Linux x86_64) gremlin-local-app"
FETCH_TIMEOUT = 60
TRANSCRIPT_LIMIT = 24 * 1024  # max chars returned to the model


class YoutubeError(Exception):
    """Raised for bad URLs, network failures, or missing subtitles."""


def _ydl_opts() -> dict:
    return {
        "skip_download": True,
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
    }


def _extract(url: str) -> dict:
    """Run yt-dlp's extractor and return the video info dict."""
    import yt_dlp  # imported lazily so tests can run without network

    try:
        with yt_dlp.YoutubeDL(_ydl_opts()) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as e:  # yt-dlp raises many exotic exception types
        raise YoutubeError(f"yt-dlp could not fetch {url}: {e}") from e
    if not info:
        raise YoutubeError(f"no info returned for {url}")
    if info.get("_type") == "playlist":
        entries = info.get("entries") or []
        if not entries:
            raise YoutubeError("playlist has no entries")
        info = entries[0]
    return info


def _pick_subtitle(info: dict, lang: str) -> tuple[dict, str]:
    """Choose the best subtitle entry for ``lang``.

    Returns ``(entry, source)`` where source is ``"manual"`` or ``"auto"``.
    Preference: manual subs in ``lang`` -> auto captions in ``lang`` ->
    any entry whose code is the base language (``en`` matches ``en-US``).
    """
    lang = (lang or "en").strip().lower()
    base = lang.split("-")[0]
    manual = info.get("subtitles") or {}
    auto = info.get("automatic_captions") or {}
    for source, table in (("manual", manual), ("auto", auto)):
        if lang in table:
            entry = _first_fetchable(table[lang])
            if entry:
                return entry, source
    for source, table in (("manual", manual), ("auto", auto)):
        for code, entries in table.items():
            c = code.lower()
            if c == base or c.startswith(base + "-"):
                entry = _first_fetchable(entries)
                if entry:
                    return entry, source
    available = sorted(set(manual) | set(auto))
    raise YoutubeError(
        f"no subtitles for language '{lang}'. Available: {', '.join(available) or '(none)'}"
    )


def _first_fetchable(entries: list[dict]) -> dict | None:
    for e in entries or []:
        if e.get("url") and e.get("ext") in ("vtt", "srt"):
            return e
    return None


_TS_RE = re.compile(r"^\d{2}:\d{2}:\d{2}[.,]\d{3} --> ")
_TAG_RE = re.compile(r"<[^>]+>")


def _parse_vtt(text: str) -> str:
    """Turn WebVTT (or SRT-ish) cues into plain transcript lines.

    YouTube auto-generated captions accumulate the current phrase across
    cues, so for each cue we keep its last (most complete) line, then drop
    any line that is a prefix of the following line (rolling duplication).
    """
    cues: list[list[str]] = []
    current: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if _TS_RE.match(line) or re.fullmatch(r"\d+", line):
            if current:
                cues.append(current)
                current = []
            continue
        if not line or line.upper().startswith(("WEBVTT", "KIND:", "ALIGN:", "LANGUAGE:", "STYLE")):
            continue
        line = _TAG_RE.sub("", line).strip()
        if line:
            current.append(line)
    if current:
        cues.append(current)
    lines = [c[-1] for c in cues]
    out: list[str] = []
    for i, ln in enumerate(lines):
        nxt = lines[i + 1] if i + 1 < len(lines) else None
        if nxt is not None and nxt.startswith(ln):
            continue  # this cue's phrase continues (or repeats) in the next cue
        out.append(ln)
    return "\n".join(out)

def _safe_name(name: str, fallback: str = "untitled", maxlen: int = 100) -> str:
    """Make a string safe for use as a single path component."""
    name = re.sub(r"[\\/:*?\"<>|\x00-\x1f]", "_", name or "").strip().strip(".")
    name = re.sub(r"\s+", " ", name)
    if len(name) > maxlen:
        name = name[:maxlen].rstrip()
    return name or fallback

def _upload_date(info: dict) -> str:
    """Return the video's upload date as ``YYYY-MM-DD``, or ``""`` if unknown.

    yt-dlp exposes ``upload_date`` as an ``YYYYMMDD`` string.
    """
    raw = str(info.get("upload_date") or "").strip()
    if len(raw) == 8 and raw.isdigit():
        return f"{raw[0:4]}-{raw[4:6]}-{raw[6:8]}"
    return ""

def fetch_transcript(url: str, lang: str = "en", limit: int = TRANSCRIPT_LIMIT, cfg=None) -> str:
    """Full pipeline: extract info, pick subtitles, download, parse, save.

    With ``cfg`` given, the full transcript is also written to
    ``files/transcripts/<channel>/<video name>.txt`` under the project root.
    """
    info = _extract(url)
    entry, source = _pick_subtitle(info, lang)
    try:
        resp = requests.get(entry["url"], timeout=FETCH_TIMEOUT, headers={"User-Agent": UA})
        resp.raise_for_status()
    except requests.RequestException as e:
        raise YoutubeError(f"could not download subtitle file: {e}") from e
    text = _parse_vtt(resp.text)
    if not text:
        raise YoutubeError("subtitle file was empty after parsing")
    title = info.get("title", "?")
    channel = info.get("channel") or info.get("uploader") or "?"
    date = _upload_date(info)
    date_line = f"\nUploaded: {date}" if date else ""
    file_header = f"Video: {title}\nChannel: {channel}{date_line}\nSource: {source} ({lang})\nURL: {url}\n\n"
    out = file_header + text
    saved = None
    if cfg is not None:
        try:
            channel_name = _safe_name(channel, "unknown-channel", 60)
            title_name = _safe_name(title, "untitled", 100)
            prefix = f"{date}_" if date else ""
            dest_dir = cfg.transcripts_dir / channel_name
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest = dest_dir / f"{prefix}{title_name}.txt"
            dest.write_text(file_header + text + "\n", encoding="utf-8")
            saved = dest.relative_to(cfg.root)
        except OSError as e:
            log.warning("could not save transcript: %s", e)
    header = f"Video: {title}\nTranscript source: {source} ({lang})"
    if saved:
        header += f"\nSaved to: {saved}"
    body = header + "\n\n" + text
    if len(body) > limit:
        body = body[:limit] + "\n[truncated]"
    return body


def fetch_video_info(url: str) -> str:
    info = _extract(url)
    secs = info.get("duration") or 0
    m, s = divmod(int(secs), 60)
    h, m = divmod(m, 60)
    dur = f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:d}:{s:02d}"
    desc = (info.get("description") or "").strip()
    if len(desc) > 1500:
        desc = desc[:1500] + "..."
    return (
        f"Title: {info.get('title', '?')}\n"
        f"Uploader: {info.get('uploader') or info.get('channel') or '?'}\n"
        f"Duration: {dur}\n"
        f"Views: {info.get('view_count', '?')}\n"
        f"Description: {desc or '(none)'}"
    )


def build_youtube_tools(cfg) -> list[Tool]:
    return [
        Tool(
            name="youtube_transcript",
            description=(
                "Fetch the transcript of a YouTube video. Uses manual subtitles when "
                "available, otherwise auto-generated captions. The transcript is also saved "
                "under files/transcripts/<channel>/<video name>.txt. Returns the save path, "
                "the title, and the full (possibly truncated) transcript text."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "YouTube video URL"},
                    "lang": {
                        "type": "string",
                        "description": "subtitle language code, e.g. 'en', 'de', 'ja' (default 'en')",
                    },
                },
                "required": ["url"],
            },
            handler=lambda a: fetch_transcript(a["url"], a.get("lang", "en"), cfg=cfg),
        ),
        Tool(
            name="youtube_video_info",
            description="Fetch title, uploader, duration, view count and description of a YouTube video.",
            parameters={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "YouTube video URL"},
                },
                "required": ["url"],
            },
            handler=lambda args: fetch_video_info(args["url"]),
        ),
    ]
