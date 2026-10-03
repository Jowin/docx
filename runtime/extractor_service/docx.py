"""Doc Parser for .docx: paragraphs and tables, read straight from the package XML.

No third-party library: a .docx is a zip whose ``word/document.xml`` holds
paragraphs (``w:p``) and tables (``w:tbl``). Locators:

    P12          the 12th body paragraph
    T2:R3C4      table 2, row 3, cell 4

Legacy binary .doc files are not read; they flag ``unsupported_attachment:doc``.
"""
from __future__ import annotations

import io
import zipfile
from dataclasses import dataclass, field
from xml.etree import ElementTree as ET

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
MAX_XML_BYTES = 50 * 1024 * 1024


@dataclass
class DocxContent:
    paragraphs: list[tuple[int, str]] = field(default_factory=list)          # (n, text)
    tables: list[list[list[str]]] = field(default_factory=list)              # table -> rows -> cells


def _text(el) -> str:
    parts = []
    for node in el.iter():
        if node.tag == f"{W}t" and node.text:
            parts.append(node.text)
        elif node.tag in (f"{W}tab",):
            parts.append("\t")
        elif node.tag in (f"{W}br", f"{W}cr"):
            parts.append(" ")
    return " ".join("".join(parts).split())


def read_docx(data: bytes) -> DocxContent:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        info = zf.getinfo("word/document.xml")
        if info.file_size > MAX_XML_BYTES:
            raise ValueError("document.xml too large")
        root = ET.fromstring(zf.read(info))
    body = root.find(f"{W}body")
    out = DocxContent()
    if body is None:
        return out
    n = 0
    for el in body:
        if el.tag == f"{W}p":
            text = _text(el)
            n += 1
            if text:
                out.paragraphs.append((n, text))
        elif el.tag == f"{W}tbl":
            rows = []
            for tr in el.iter(f"{W}tr"):
                rows.append([_text(tc) for tc in tr.findall(f"{W}tc")])
            out.tables.append(rows)
    return out
