"""The writer tools by format: ``write(result, "csv" | "xlsx" | "docx" | "pdf") -> (bytes, media type, ext)``."""
from __future__ import annotations

from typing import Any, Callable

from . import csv_writer, docx_writer, excel_writer, pdf_writer
from .common import PARAM_INVALID, ToolError

#: format -> (writer, media type, file extension)
WRITERS: dict[str, tuple[Callable[..., bytes], str, str]] = {
    "csv": (csv_writer.write_csv, csv_writer.MEDIA_TYPE, csv_writer.EXTENSION),
    "xlsx": (excel_writer.write_xlsx, excel_writer.MEDIA_TYPE, excel_writer.EXTENSION),
    "docx": (docx_writer.write_docx, docx_writer.MEDIA_TYPE, docx_writer.EXTENSION),
    "pdf": (pdf_writer.write_pdf, pdf_writer.MEDIA_TYPE, pdf_writer.EXTENSION),
}
ALIASES = {"excel": "xlsx", "xls": "xlsx", "word": "docx", "doc": "docx"}


def normalise(fmt: str) -> str:
    f = ALIASES.get(str(fmt).lower().lstrip("."), str(fmt).lower().lstrip("."))
    if f not in WRITERS:
        raise ToolError(PARAM_INVALID, f"unknown output format {fmt!r}; use one of {', '.join(WRITERS)}",
                        detail={"formats": list(WRITERS)})
    return f


def write(result: Any, fmt: str, **params: Any) -> tuple[bytes, str, str]:
    f = normalise(fmt)
    fn, media, ext = WRITERS[f]
    return fn(result, **params), media, ext
