"""EPUB parser: zip + lxml, no ebooklib.

Walks the spine in reading order and emits markdown: headings become ``#``
levels, everything else becomes paragraphs. Chapters are separated by
``<!--page:N-->`` (N = spine position) so the chunker can cite "ch./part N".
"""
from __future__ import annotations

import zipfile
from pathlib import Path

from lxml import etree

from ..metadata import derive_metadata
from .models import ParsedDoc, doc_id_for, sha256_of

_NS_XHTML = "http://www.w3.org/1999/xhtml"
_NS_OPF = "http://www.idpf.org/2007/opf"
_HEADINGS = {f"h{i}": i for i in range(1, 7)}
_BLOCK_TAGS = {t.lower(): t.lower() for t in (
    "p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "blockquote", "pre",
    "figcaption", "dt", "dd",
)}


def _local(tag) -> str:
    return etree.QName(tag).localname if isinstance(tag, str) else ""


def _block_text(el) -> str:
    """Whitespace-normalized text of one block element."""
    return " ".join("".join(el.itertext()).split())


def _render_document(root, parts: list[str]) -> None:
    for el in root.iter():
        name = _local(el.tag)
        if name not in _BLOCK_TAGS:
            continue
        # skip nested blocks (their parent was already emitted)
        if any(_local(a.tag) in _BLOCK_TAGS for a in el.iterancestors()):
            continue
        text = _block_text(el)
        if not text:
            continue
        level = _HEADINGS.get(name)
        parts.append(f"{'#' * level} {text}" if level else text)


def _spine_order(zf: zipfile.ZipFile) -> list[str]:
    """Return content-document hrefs (slash-form) in spine reading order."""
    names = zf.namelist()
    opf_name = next(
        (n for n in names if n.lower().endswith(".opf")),
        next((n for n in names if n == "META-INF/container.xml"), None),
    )
    if opf_name is None or opf_name.endswith("container.xml"):
        # fall back: every xhtml/html document, sorted
        return sorted(
            n for n in names
            if n.lower().endswith((".xhtml", ".html", ".htm"))
            and "images" not in n.lower()
        )
    tree = etree.fromstring(zf.read(opf_name))
    base = str(Path(opf_name).parent)

    manifest = {}
    for item in tree.iter(f"{{{_NS_OPF}}}item"):
        href = item.get("href")
        media = (item.get("media-type") or "").lower()
        if href and ("html" in media or "xml" in media
                     or href.lower().endswith((".xhtml", ".html", ".htm"))):
            manifest[item.get("id")] = (base + "/" + href).replace("//", "/") \
                if base else href

    ordered: list[str] = []
    for ref in tree.iter(f"{{{_NS_OPF}}}itemref"):
        href = manifest.get(ref.get("idref"))
        if href:
            ordered.append(href)
    return ordered or [
        n for n in names
        if n.lower().endswith((".xhtml", ".html", ".htm"))
    ]


def parse_epub_file(path: Path, source_root: Path) -> ParsedDoc:
    """Parse an EPUB book to markdown."""
    with zipfile.ZipFile(path) as zf:
        order = _spine_order(zf)
        parts: list[str] = []
        for idx, href in enumerate(order, start=1):
            try:
                data = zf.read(href)
            except KeyError:
                continue
            try:
                root = etree.fromstring(data)
            except etree.XMLSyntaxError:
                continue
            parts.append(f"<!--page:{idx}-->")
            _render_document(root, parts)
    markdown = "\n\n".join(p for p in parts if p).strip()
    if not markdown:
        from .models import IngestError
        raise IngestError(f"{path.name}: no readable content documents in EPUB")
    return ParsedDoc(
        doc_id=doc_id_for(path.relative_to(source_root).as_posix()),
        source_path=path.relative_to(source_root).as_posix(),
        source_type="epub",
        size_bytes=path.stat().st_size,
        markdown=markdown,
        sha256=sha256_of(path),
        meta=derive_metadata(path),
        page_count=len(order),
    )
