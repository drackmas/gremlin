"""Markdown -> plain speech text for Piper TTS.

Assistant replies are markdown; Piper would happily pronounce ``**bold**`` and
``[text](url)``.  :func:`sanitize_for_speech` rewrites that syntax into prose
an TTS engine can read naturally.  It is deliberately conservative: ordinary
prose punctuation (periods, commas, question marks) is never touched, and
content is only dropped when its meaning is carried by syntax (fenced code,
URLs, HTML).

Rules (applied in order):

- Fenced code blocks (``` or ~~~) -> single ``Code block.`` marker; the body
  and language tag are dropped.  A fence line with no open fence (e.g. the
  closing fence of a block split across streamed chunks) is treated as an
  opener, so the code after it is dropped and any prose before the fence is
  kept.
- ``<script>`` / ``<style>`` blocks -> removed entirely (content included);
  any other HTML tags -> single space.
- Headings (``#``..``###### ``) -> drop the markers, keep the text.
- Horizontal rules (``---``/``***``/``___`` lines) -> removed.
- Blockquotes (``> ``) -> drop the prefix, keep the text.
- List markers (``- ``/``* ``/``+ ``/``1. ``) -> drop, keep item text.
- Emphasis (``**``, ``__``, single ``*``/``_``) -> keep the inner text.
  Word-internal underscores (``snake_case``) are not emphasis and survive.
  Unmatched ``**`` pairs (e.g. emphasis split across streamed chunks) are
  dropped, so stray asterisks are never spoken.
  Word-internal underscores (``snake_case``) are not emphasis and survive.
- Inline code (backtick spans) -> keep the content, drop the backticks; any
  leftover stray backticks are stripped.
- Images ``![alt](url)`` -> ``alt`` (or ``image`` when there is no alt text).
- Links ``[text](url)`` -> ``text`` (the URL is never spoken).
- Remaining raw ``http(s)://`` URLs -> single ``link`` word (run last, so
  URLs inside link/image syntax are already gone with the syntax).
- All whitespace runs (spaces, tabs, newlines) collapse to a single space.

Tables (``| a | b |``) are out of scope: the pipes survive as text and the
cells are spoken in order, which is acceptable for speech.

The function is pure and idempotent.
"""

from __future__ import annotations

import re

# --- line-anchored structures (re.MULTILINE) --------------------------------

_FENCE = re.compile(r"^\s{0,3}(```|~~~)", re.M)

_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+", re.M)

_HRULE = re.compile(r"^\s{0,3}(?:-{3,}|\*{3,}|_{3,})\s*$", re.M)

_BLOCKQUOTE = re.compile(r"^\s{0,3}>\s?", re.M)

_OLIST = re.compile(r"^\s{0,3}\d+\.\s+", re.M)

_ULIST = re.compile(r"^\s{0,3}[-*+]\s+", re.M)

# --- inline syntax -----------------------------------------------------------

_SCRIPT_STYLE = re.compile(r"<(script|style)\b[^>]*>.*?</\1>", re.I | re.S)

_HTML_TAG = re.compile(r"</?[a-zA-Z][^<>]*>")

_BOLD = re.compile(r"(\*\*|__)(.+?)\1")

# Single-star / single-underscore emphasis, word-safe: the opening marker
# must not follow a word char (so ``snake_case`` and ``2*3*4`` survive) and
# the content must not start or end with whitespace.
_ITALIC = re.compile(r"(?<![\w*])(\*|_)([^\s*_](?:.*?[^\s*_]?)?)(\1)(?![\w*])")

# Emphasis markers with no matching pair: dropped rather than spoken as
# "asterisk asterisk" (e.g. ``**bold`` when the closing pair never arrived).
_LEFTOVER_BOLD = re.compile(r"\*\*")

_INLINE_CODE = re.compile(r"`([^`]*)`")

_IMAGE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")

_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")

_URL = re.compile(r"https?://\S+")

_NL = re.compile(r"\n+")
_WS = re.compile(r"[ \t]+")


def _drop_fenced_code(out: str) -> str:
    """Replace every fenced code block with a single ``Code block.`` marker.

    A fence match that is not closing an open fence is treated as an opener
    (chunk-streaming case: the opener arrived in an earlier chunk).  The
    region from the opener to its closer (or to the end of the chunk when
    unclosed) is dropped.
    """
    parts: list[str] = []
    pos = 0
    fence_start = 0
    in_fence = False
    for m in _FENCE.finditer(out):
        if not in_fence:
            fence_start = m.start()
            in_fence = True
        else:
            parts.append(out[pos:fence_start])
            parts.append("Code block.")
            pos = m.end()
            in_fence = False
    if in_fence:
        parts.append(out[pos:fence_start])
        parts.append("Code block.")
    else:
        parts.append(out[pos:])
    return "".join(parts)


def sanitize_for_speech(text: str) -> str:
    """Return *text* rewritten for speech synthesis (see module docstring)."""
    if not text:
        return ""

    out = _drop_fenced_code(text)
    out = _SCRIPT_STYLE.sub(" ", out)
    out = _HTML_TAG.sub(" ", out)
    out = _HEADING.sub("", out)
    out = _HRULE.sub("", out)
    out = _BLOCKQUOTE.sub("", out)
    out = _ULIST.sub("", out)
    out = _OLIST.sub("", out)
    out = _BOLD.sub(r"\2", out)
    out = _ITALIC.sub(r"\2", out)
    out = _LEFTOVER_BOLD.sub(" ", out)
    out = _INLINE_CODE.sub(r"\1", out)
    out = out.replace("`", "")
    out = _IMAGE.sub(lambda m: m.group(1) or "image", out)
    out = _LINK.sub(r"\1", out)
    out = _URL.sub(" link", out)

    out = _NL.sub(" ", out)
    out = _WS.sub(" ", out)
    return out.strip()
