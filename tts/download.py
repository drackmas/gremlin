"""Download Piper voice files from Hugging Face into the models/piper dir.

Each voice in :data:`tts.VOICES` is two files — ``<id>.onnx`` (weights,
~60 MB for medium) and ``<id>.onnx.json`` (small config) — served from the
``rhasspy/piper-voices`` repository (v1.0.0 layout
``<lang>/<locale>/<name>/<quality>/<file>``). Files that already exist are
skipped, so a re-run only fetches what is missing.

Usage:
    python -m tts.download                 # every voice in the catalog
    python -m tts.download en_US-amy-medium ...
"""

from __future__ import annotations

import logging
import os
import urllib.request
from pathlib import Path

from .piper_tts import voice_by_id

log = logging.getLogger("gremlin.tts")

#: Base URL of the piper-voices release (v1.0.0 layout).
VOICE_BASE_URL = "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0"


def voice_url_parts(voice_id: str) -> tuple[str, str, str, str]:
    """Split a voice id like ``en_GB-alba-medium`` into its URL components:
    ``(lang, locale, name, quality)`` = ``("en", "en_GB", "alba", "medium")``.

    Raises ``ValueError`` for an id not in the catalog.
    """
    voice = voice_by_id(voice_id)  # validates membership
    locale, name, quality = voice.id.rsplit("-", 2)
    lang = locale.split("_", 1)[0]
    return lang, locale, name, quality


def voice_urls(voice_id: str) -> tuple[str, str]:
    """Hugging Face URLs of the ``.onnx`` and ``.onnx.json`` files."""
    lang, locale, name, quality = voice_url_parts(voice_id)
    base = f"{VOICE_BASE_URL}/{lang}/{locale}/{name}/{quality}"
    return f"{base}/{voice_id}.onnx", f"{base}/{voice_id}.onnx.json"


def download_voice(
    voice_id: str,
    dest_dir: str | Path,
    opener=None,
) -> dict[str, str]:
    """Fetch a voice's ``.onnx`` + ``.onnx.json`` into *dest_dir*.

    Files already present are skipped (no HTTP request). *opener* is an
    ``urlopen``-compatible callable, injectable for tests. Returns a mapping
    of file name -> ``"ok"`` (downloaded) or ``"skipped"`` (already present).

    Raises ``ValueError`` for an unknown voice id and ``OSError`` if a
    download fails.
    """
    if opener is None:
        opener = urllib.request.urlopen
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    out: dict[str, str] = {}
    for url, fname in zip(voice_urls(voice_id), (f"{voice_id}.onnx", f"{voice_id}.onnx.json")):
        target = dest / fname
        if target.exists():
            log.info("skip (exists): %s", target)
            out[fname] = "skipped"
            continue
        log.info("downloading: %s", url)
        with opener(url) as resp, open(target, "wb") as fh:
            fh.write(resp.read())
        out[fname] = "ok"
    return out


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: download the catalog (or the named voices)."""
    import argparse

    from .piper_tts import VOICES

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "voices",
        nargs="*",
        metavar="VOICE_ID",
        help="voice ids to download (default: every voice in the catalog)",
    )
    args = parser.parse_args(argv)
    ids = args.voices or [v.id for v in VOICES]

    root = Path(os.environ.get("GREMLIN_ROOT", Path(__file__).resolve().parent.parent))
    dest = root / "models" / "piper"
    for voice_id in ids:
        download_voice(voice_id, dest)
        print(f"done: {voice_id} -> {dest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
