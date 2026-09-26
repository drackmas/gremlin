"""Download Whisper ONNX models from Hugging Face into ``models/stt/``.

Each catalog model (:data:`stt.MODELS`) is five files served from its
``onnx-community`` repository — ``config.json``, ``vocab.json``,
``added_tokens.json`` and the two ``onnx/*.onnx`` weights. Files that already
exist are skipped, so a re-run only fetches what is missing.

Usage:
    python -m stt.download                  # every model in the catalog
    python -m stt.download whisper-tiny     # just the named models

Alternatively, ``huggingface_hub`` (installed with the ``onnx-asr[hub]``
extra) can fetch the same files on demand; see ``models/stt/README.md``.
"""

from __future__ import annotations

import logging
import os
import urllib.request
from pathlib import Path

from .models import MODELS, model_by_id

log = logging.getLogger("gremlin.stt")

#: Base URL for Hugging Face repository files.
STT_BASE_URL = "https://huggingface.co"


def model_url(model_id: str, filename: str) -> str:
    """Hugging Face URL of one file of a catalog model."""
    model = model_by_id(model_id)  # validates membership
    return f"{STT_BASE_URL}/{model.repo_id}/resolve/main/{filename}"


def model_urls(model_id: str) -> list[tuple[str, str]]:
    """(file name, URL) pairs for every file of a catalog model, in order."""
    model = model_by_id(model_id)
    return [(f, model_url(model.id, f)) for f in model.files]


def download_model(
    model_id: str,
    dest_dir: str | Path,
    opener=None,
) -> dict[str, str]:
    """Fetch a model's files into ``<dest_dir>/<model_id>/``.

    Files already present are skipped (no HTTP request). *opener* is an
    ``urlopen``-compatible callable, injectable for tests. Returns a mapping
    of file name -> ``"ok"`` (downloaded) or ``"skipped"`` (already present).

    Raises:
        ValueError: for an unknown model id.
        OSError: if a download fails.
    """
    if opener is None:
        opener = urllib.request.urlopen
    dest = Path(dest_dir) / model_by_id(model_id).id
    dest.mkdir(parents=True, exist_ok=True)
    out: dict[str, str] = {}
    for fname, url in model_urls(model_id):
        target = dest / fname
        if target.exists():
            log.info("skip (exists): %s", target)
            out[fname] = "skipped"
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        log.info("downloading: %s", url)
        with opener(url) as resp, open(target, "wb") as fh:
            fh.write(resp.read())
        out[fname] = "ok"
    return out


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: download the catalog (or the named models)."""
    import argparse

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "models",
        nargs="*",
        metavar="MODEL_ID",
        help="model ids to download (default: every model in the catalog)",
    )
    args = parser.parse_args(argv)
    ids = args.models or [m.id for m in MODELS]

    root = Path(os.environ.get("GREMLIN_ROOT", Path(__file__).resolve().parent.parent))
    dest = root / "models" / "stt"
    for model_id in ids:
        download_model(model_id, dest)
        print(f"done: {model_id} -> {dest / model_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
