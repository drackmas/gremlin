"""Human-readable citations, generalized by source type.

    book/epub/pdf -> "Manners of Women (1908), p. 42, 'Heading'"
    video/audio   -> "Title (2024-03-12), 14:32, section 'Heading'"
    transcript    -> "Title (2024-03-12), section 'Heading'"
    doc/docx/txt  -> "report.docx, 'Section 2'"  (char-offset fallback)

Degrades gracefully: whatever is missing (date, page, timestamp, heading)
is simply left out.
"""
from __future__ import annotations

import sqlite3

_MAX_HEADING = 60


def _heading(parent: sqlite3.Row | None) -> str:
    if parent is not None and parent["heading"]:
        h = str(parent["heading"]).strip()
        return h[:_MAX_HEADING] + ("…" if len(h) > _MAX_HEADING else "")
    return ""


def render_citation(doc: sqlite3.Row, parent: sqlite3.Row | None) -> str:
    """One clean citation string for a hit."""
    title = (doc["title"] or doc["source_path"]).strip()
    date = (doc["date"] or "").strip()
    stype = doc["source_type"]
    h = _heading(parent)

    if stype in ("audio", "video"):
        base = f"{title} ({date})" if date else title
        ts = parent["timestamp"] if parent is not None and parent["timestamp"] else ""
        if ts:
            s = f"{base}, {ts}"
        else:
            s = base
        if h:
            s += f", section '{h}'"
        return s

    if stype in ("pdf", "epub"):
        year = date.split("-")[0]
        base = f"{title} ({year})" if year else title
        s = base
        if parent is not None and parent["page"]:
            s += f", p. {parent['page']}"
        if h:
            s += f", '{h}'"
        return s

    if stype == "transcript":
        base = f"{title} ({date})" if date else title
        s = base
        if h:
            s += f", section '{h}'"
        return s

    # doc, docx, txt, and anything else
    s = doc["source_path"]
    if h:
        s += f", '{h}'"
    return s
