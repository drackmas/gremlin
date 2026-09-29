"""Structure-aware chunking: parents (structural units) and children (chunks).

Parents are the units a citation can point at:
  * a PDF/EPUB page            (``<!--page:N-->``)
  * a heading section          (``# .. ######``)
  * a media timestamp segment  (``[HH:MM:SS]`` lines)
  * otherwise the whole document

Children are <= ``chunk_max_chars`` windows packed from sentence/word
boundaries with ``chunk_overlap_chars`` overlap. Tiny parents (a bare
heading, a one-line fragment) are merged into the previous parent when they
share its page/timestamp boundary, so citations never point at nothing.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .config import KnowledgeConfig

_PAGE_RE = re.compile(r"^<!--page:(\d+)>-->")
_HEAD_RE = re.compile(r"^(#{1,6})\s+(.*\S)\s*$")
_TS_RE = re.compile(r"^\[(\d{2}:\d{2}:\d{2})\]\s*(.*\S)?\s*$")
_SENT_GAP_RE = re.compile(r"(?<=[.!?])\s+")
_WS_RE = re.compile(r"\s+")


@dataclass
class Parent:
    parent_id: str
    doc_id: str
    text: str
    page: int | None = None
    timestamp: str | None = None
    heading: str | None = None
    char_start: int = 0
    char_end: int = 0


@dataclass
class Chunk:
    chunk_id: str
    parent_id: str
    doc_id: str
    text: str
    char_start: int = 0
    char_end: int = 0
    ocr_suspect: bool = False


def _line_offsets(lines: list[str]) -> list[int]:
    offs = [0]
    for ln in lines:
        offs.append(offs[-1] + len(ln) + 1)
    return offs


def _find_units(lines: list[str], offs: list[int]) -> list[dict]:
    """Split markdown lines into structural units.

    A unit starts at a page marker, a heading, or a timestamp line and runs
    to the next such line. Marker-only units (empty content) are dropped and
    their boundary metadata is folded into the next real unit.
    """
    boundaries: list[tuple[int, int | None, str | None, str | None]] = []
    for i, line in enumerate(lines):
        mp = _PAGE_RE.match(line)
        if mp:
            boundaries.append((i, int(mp.group(1)), None, None))
            continue
        mh = _HEAD_RE.match(line)
        if mh:
            boundaries.append((i, None, None, mh.group(2)))
            continue
        mt = _TS_RE.match(line)
        if mt:
            boundaries.append((i, None, mt.group(1), None))

    if not boundaries:
        if not any(ln.strip() for ln in lines):
            return []
        return [
            dict(
                start=0, end=len(lines), page=None, ts=None, heading=None,
                text="\n".join(lines).strip(),
                char_start=0, char_end=max(0, offs[-1] - 1),
            )
        ]

    units: list[dict] = []
    pending = dict(page=None, ts=None, heading=None)
    for j, (start, page, ts, heading) in enumerate(boundaries):
        end = boundaries[j + 1][0] if j + 1 < len(boundaries) else len(lines)
        body = [ln for ln in lines[start:end] if not _PAGE_RE.match(ln)]
        text = "\n".join(body).strip()
        if not text:
            if page is not None:
                pending["page"] = page  # type: ignore[assignment]
            if ts is not None:
                pending["ts"] = ts  # type: ignore[assignment]
            if heading:
                pending["heading"] = heading  # type: ignore[assignment]
            continue
        units.append(
            dict(
                page=page if page is not None else pending["page"],
                ts=ts if ts is not None else pending["ts"],
                heading=heading if heading else pending["heading"],
                text=text,
                char_start=offs[start],
                char_end=offs[min(end, len(lines))] - 1,
            )
        )
        pending = dict(page=None, ts=None, heading=None)
    return units


def _merge_tiny_units(units: list[dict], cfg: KnowledgeConfig) -> list[dict]:
    """Merge units smaller than ``chunk_min_chars`` into the previous one
    when they share the same page/timestamp boundary."""
    out: list[dict] = []
    for u in units:
        if out and len(u["text"]) < cfg.chunk_min_chars:
            prev = out[-1]
            if u["page"] == prev["page"] and u["ts"] == prev["ts"]:
                prev["text"] = (prev["text"] + "\n\n" + u["text"]).strip()
                prev["char_end"] = u["char_end"]
                if u["heading"]:
                    prev["heading"] = u["heading"]
                continue
        out.append(u)
    return out


def _split_text(text: str, cfg: KnowledgeConfig) -> list[tuple[str, int, int]]:
    """Greedily pack *text* into (piece, start, end) windows.

    A window ends at the sentence cut nearest ``start + max`` (never shorter
    than 60% of max), else at a word boundary near max. The next window
    starts up to ``chunk_overlap_chars`` back, snapped to a word boundary.
    """
    if len(text) <= cfg.chunk_max_chars:
        return [(text, 0, len(text))]
    pieces: list[tuple[str, int, int]] = []
    start = 0
    while start < len(text):
        target = start + cfg.chunk_max_chars
        if target >= len(text):
            pieces.append((text[start:], start, len(text)))
            break
        window = text[start:target + 1]
        cuts = [m.end() for m in _SENT_GAP_RE.finditer(window)]
        floor = start + int(cfg.chunk_max_chars * 0.6)
        candidates = [start + c for c in cuts if start + c <= target]
        if candidates and candidates[-1] >= floor:
            end = candidates[-1]
        else:
            cut = window.rfind(" ", 0, len(window))
            end = start + (cut if cut > 0 else cfg.chunk_max_chars)
        if end <= start:
            end = min(start + cfg.chunk_max_chars, len(text))
        pieces.append((text[start:end], start, end))
        if end >= len(text):
            break
        ov_start = max(start + 1, end - cfg.chunk_overlap_chars)
        ws = [m.start() + ov_start for m in _WS_RE.finditer(text[ov_start:end])]
        start = max(ws) if ws else end
        if start <= pieces[-1][1]:
            start = end
    return pieces


def chunk_markdown(
    doc_id: str, markdown: str, cfg: KnowledgeConfig, ocr_suspect: bool = False
) -> tuple[list[Parent], list[Chunk]]:
    """Chunk one document's markdown into parents and child chunks."""
    lines = markdown.splitlines()
    offs = _line_offsets(lines)
    units = _merge_tiny_units(_find_units(lines, offs), cfg)

    parents: list[Parent] = []
    chunks: list[Chunk] = []
    ci = 0
    for pi, u in enumerate(units):
        pid = f"{doc_id}-p{pi:04d}"
        parents.append(
            Parent(
                parent_id=pid,
                doc_id=doc_id,
                text=u["text"],
                page=u["page"],
                timestamp=u["ts"],
                heading=u["heading"],
                char_start=u["char_start"],
                char_end=u["char_end"],
            )
        )
        for text, s, e in _split_text(u["text"], cfg):
            chunks.append(
                Chunk(
                    chunk_id=f"{doc_id}-c{ci:05d}",
                    parent_id=pid,
                    doc_id=doc_id,
                    text=text,
                    char_start=u["char_start"] + s,
                    char_end=u["char_start"] + e,
                    ocr_suspect=ocr_suspect,
                )
            )
            ci += 1
    return parents, chunks
