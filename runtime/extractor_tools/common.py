"""Shared plumbing for the Layer 4 reader tools.

Every tool here is pure (RT-27): bytes + params in, JSON-native dict out.
No clock, no filesystem, no network, no model call. The same input always
produces byte-identical JSON, which the tests assert.
"""
from __future__ import annotations

import hashlib
import re
from typing import Any

TOOL_VERSION = "0.3.0"

# Default guard rails. Callers may lower them; the API never raises them
# above these without an explicit parameter.
DEFAULT_MAX_BYTES = 10 * 1024 * 1024          # per attachment
DEFAULT_MAX_UNCOMPRESSED = 200 * 1024 * 1024  # zip-bomb guard for xlsx


class ToolError(Exception):
    """Typed tool failure (RT-29).

    ``permanent`` tells the Retry Controller whether retrying can help.
    These tools work on in-memory bytes, so nearly every failure is
    permanent; transient failures (e.g. fetching the bytes from S3) belong
    to the caller that resolved the attachment reference.
    """

    def __init__(self, code: str, message: str, *, permanent: bool = True,
                 detail: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.permanent = permanent
        self.detail = detail or {}

    def to_dict(self) -> dict[str, Any]:
        return {"error": self.code, "permanent": self.permanent,
                "message": str(self), "detail": self.detail}


# Closed set of error codes emitted by these tools.
FORMAT_MISMATCH = "format_mismatch"      # bytes are not the format this tool reads
FILE_CORRUPT = "file_corrupt"            # right format, unreadable content
FILE_ENCRYPTED = "file_encrypted"        # needs the Decryption Tool first
INPUT_TOO_LARGE = "input_too_large"      # exceeds a size / row / page guard
SHEET_NOT_FOUND = "sheet_not_found"
RANGE_INVALID = "range_invalid"
PARAM_INVALID = "param_invalid"


def envelope(tool: str, data: bytes, filename: str | None,
             params: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    """Wrap a result with what is needed to reproduce it."""
    return {
        "tool": tool,
        "tool_version": TOOL_VERSION,
        "input": {"filename": filename, "bytes": len(data),
                  "sha256": hashlib.sha256(data).hexdigest()},
        "params": params,
        "result": result,
    }


def check_size(data: bytes, max_bytes: int) -> None:
    if not data:
        raise ToolError(FILE_CORRUPT, "empty input")
    if len(data) > max_bytes:
        raise ToolError(INPUT_TOO_LARGE, f"input is {len(data)} bytes, limit {max_bytes}",
                        detail={"bytes": len(data), "limit": max_bytes})


# ---------------------------------------------------------------- A1 helpers

def col_letter(col: int) -> str:
    """1 -> A, 27 -> AA."""
    if col < 1:
        raise ValueError(col)
    out = ""
    while col:
        col, rem = divmod(col - 1, 26)
        out = chr(65 + rem) + out
    return out


def col_index(letters: str) -> int:
    n = 0
    for ch in letters.upper():
        n = n * 26 + (ord(ch) - 64)
    return n


def a1(row: int, col: int) -> str:
    return f"{col_letter(col)}{row}"


_A1_RANGE = re.compile(r"^\$?([A-Za-z]{1,3})\$?(\d+)(?::\$?([A-Za-z]{1,3})\$?(\d+))?$")


def parse_a1_range(text: str) -> tuple[int, int, int, int]:
    """'B2:D10' -> (min_row, min_col, max_row, max_col), all 1-based."""
    m = _A1_RANGE.match(text.strip())
    if not m:
        raise ToolError(RANGE_INVALID, f"not an A1 range: {text!r}")
    c1, r1 = col_index(m.group(1)), int(m.group(2))
    c2 = col_index(m.group(3)) if m.group(3) else c1
    r2 = int(m.group(4)) if m.group(4) else r1
    if r1 < 1 or r2 < 1:
        raise ToolError(RANGE_INVALID, f"row numbers start at 1: {text!r}")
    return min(r1, r2), min(c1, c2), max(r1, r2), max(c1, c2)


def sniff_kind(data: bytes) -> str:
    """Coarse file kind from magic bytes, for reporting embedded files.

    The Attachment Router makes the real decision; this only labels what a
    tool found so the router can be pointed at it.
    """
    if data.startswith(b"%PDF-") or b"%PDF-" in data[:1024]:
        return "pdf"
    if data.startswith(b"PK\x03\x04"):
        return "zip"               # xlsx, docx and plain zip all start this way
    if data.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        return "ole2"              # xls, doc, msg, or an OLE wrapper
    if data.startswith((b"\x89PNG", b"\xff\xd8\xff", b"GIF8", b"II*\x00", b"MM\x00*")):
        return "image"
    head = data[:2048].lstrip().lower()
    if head.startswith((b"received:", b"from:", b"return-path:", b"mime-version:",
                        b"message-id:", b"delivered-to:")):
        return "email"
    return "unknown"
