"""Document metadata from filename conventions and .meta sidecars.

Convention, in order of preference (greppable from the folder alone):
1. FILENAME as the default: ``manners-of-women-1908-ch3.txt`` -> title,
   date parsed from the name. Transcript style ``2021-09-03_Title.txt``
   yields date + title. Zero extra work.
2. SIDECAR as the escape hatch: ``<same-name>.meta`` with lines like
   ``author: ...`` / ``date: 1908`` / ``topic: etiquette``. Only when the
   filename is not enough.
3. Direct DB edits are allowed for one-offs but are NOT a feature.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

_DATE_PREFIX = re.compile(r"^(\d{4})[-_](\d{2})[-_](\d{2})(?:[_-](.*))?$")
_YMD = re.compile(r"\b(20\d{2}|19\d{2})(\d{2})(\d{2})\b")
_YEAR = re.compile(r"\b(1[5-9]\d{2}|20\d{2})\b")


@dataclass
class DocumentMeta:
    title: str
    author: str = ""
    date: str = ""      # ISO-ish: "1908", "2020-02-14", or "2021-09-03"
    topic: str = ""
    extra: dict[str, str] = field(default_factory=dict)

    def citation_year(self) -> str:
        return self.date.split("-")[0] if self.date else ""


def prettify(stem: str) -> str:
    """``manners_of_women-1908`` -> ``manners of women 1908``."""
    s = re.sub(r"[_\-]+", " ", stem)
    s = re.sub(r"\s+", " ", s).strip(" .")
    return s


def parse_filename(stem: str) -> tuple[str, str]:
    """Return ``(title, date)`` parsed from a filename stem."""
    m = _DATE_PREFIX.match(stem)
    if m:
        date = f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
        rest = m.group(4) or ""
        return prettify(rest), date
    title = prettify(stem)
    m = _YMD.search(stem)
    if m:
        return title, f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    m = _YEAR.search(stem)
    if m:
        return title, m.group(1)
    return title, ""


def read_sidecar(path: Path) -> dict[str, str]:
    """Parse ``<name>.meta``: one ``key: value`` per line."""
    sidecar = path.with_suffix(path.suffix + ".meta")
    out: dict[str, str] = {}
    if not sidecar.is_file():
        return out
    for line in sidecar.read_text(encoding="utf-8", errors="replace").splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            k, v = k.strip().lower(), v.strip()
            if k:
                out[k] = v
    return out


def derive_metadata(path: Path) -> DocumentMeta:
    """Metadata for *path* from its filename plus an optional .meta sidecar."""
    title, date = parse_filename(path.stem)
    side = read_sidecar(path)
    known = {"title", "author", "date", "topic"}
    extra = {k: v for k, v in side.items() if k not in known}
    return DocumentMeta(
        title=side.get("title") or title,
        author=side.get("author", ""),
        date=side.get("date") or date,
        topic=side.get("topic", ""),
        extra=extra,
    )
