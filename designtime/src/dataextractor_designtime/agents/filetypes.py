"""Content-based file type detection (DT-06, mirrors RT-14).

Extensions lie; a `.xlsx` that is really a zip of CSVs, or a `.csv` that is
really an Excel file, must be detected by what the bytes say.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
XLS = "application/vnd.ms-excel"
CSV = "text/csv"
PDF = "application/pdf"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
ZIP = "application/zip"
UNKNOWN = "application/octet-stream"


def detect(path: str | Path) -> str:
    p = Path(path)
    try:
        head = p.open("rb").read(8)
    except OSError:
        return UNKNOWN

    if head.startswith(b"%PDF-"):
        return PDF
    if head.startswith(b"\xd0\xcf\x11\xe0"):  # OLE2 compound file
        return XLS
    if head.startswith(b"PK\x03\x04"):
        # Both xlsx and docx are zips; the member list tells them apart.
        try:
            with zipfile.ZipFile(p) as zf:
                names = set(zf.namelist())
        except zipfile.BadZipFile:
            return ZIP
        if any(n.startswith("xl/") for n in names):
            return XLSX
        if any(n.startswith("word/") for n in names):
            return DOCX
        return ZIP

    # Text-ish: treat as CSV only if it decodes and the first line separates.
    try:
        first = p.open("r", encoding="utf-8-sig", errors="strict").readline()
    except (UnicodeDecodeError, OSError):
        return UNKNOWN
    if first and ("," in first or ";" in first or "\t" in first):
        return CSV
    return UNKNOWN


SUPPORTED_PHASE_1 = frozenset({CSV, XLSX, XLS})
