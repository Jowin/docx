"""CSV Reader tool: CSV bytes -> column profile + one page of typed rows.

Architecture Layer 4. Used by the CSV Parser Agent and the Field Mapping
Skill at runtime, and by the Corpus Profiler at design time (DT-06).

What it decides mechanically:  encoding, delimiter, cell types, and
column-level evidence (decimal mark, day/month order).
What it leaves to skills:      which column is which field, which row is
the total, what "$" means. Ambiguity is reported, never resolved by guess.

Large files (v0.2): the whole file is profiled, but only one page of rows
is returned (``row_offset`` / ``row_limit``), plus the last ``tail_rows``
data rows because totals usually sit at the bottom. Nothing typed is held
for rows outside the page, so memory and output size stay bounded by the
page, not the file. Callers page with ``page.next_offset``.

Cell refs are Excel A1 on the record grid (header = row 1 by default), so a
reviewer can open the file in Excel and land on the cell a value came from.
"""
from __future__ import annotations

import csv
import io
from collections import Counter, deque
from typing import Any, Iterator

from .common import (DEFAULT_MAX_BYTES, FORMAT_MISMATCH, INPUT_TOO_LARGE, PARAM_INVALID,
                     ToolError, a1, check_size, col_letter, envelope)
from .values import (column_type, date_order_vote, decimal_sep_vote, resolve_votes,
                     type_text)

TOOL = "csv_reader"
_SNIFF_BYTES = 64 * 1024
_DELIMITERS = ",;\t|"
_BINARY_MAGIC = {b"PK\x03\x04": "zip/xlsx", b"\xd0\xcf\x11\xe0": "ole2/xls",
                 b"%PDF-": "pdf", b"\x1f\x8b": "gzip"}
MAX_PAGE = 5_000

csv.field_size_limit(10 * 1024 * 1024)


def _decode(data: bytes) -> tuple[str, str]:
    for magic, kind in _BINARY_MAGIC.items():
        if data.startswith(magic):
            raise ToolError(FORMAT_MISMATCH, f"input is {kind}, not CSV",
                            detail={"detected": kind})
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8", errors="strict"), "utf-8-sig"
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16"), "utf-16"
    if b"\x00" in data[:_SNIFF_BYTES]:
        raise ToolError(FORMAT_MISMATCH, "NUL bytes without a UTF-16 BOM; not a text file")
    for enc in ("utf-8", "cp1252"):
        try:
            return data.decode(enc, errors="strict"), enc
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1"), "latin-1"   # total: every byte maps


def _dialect(text: str, delimiter: str | None):
    if delimiter:
        class Fixed(csv.excel):
            pass
        Fixed.delimiter = delimiter
        return Fixed
    try:
        return csv.Sniffer().sniff(text[:_SNIFF_BYTES], delimiters=_DELIMITERS)
    except csv.Error:
        return csv.excel


def _records(text: str, dialect) -> Iterator[tuple[int, list[str]]]:
    """(1-based record number, fields). Re-parsed per pass instead of stored."""
    for n, rec in enumerate(csv.reader(io.StringIO(text, newline=""), dialect), start=1):
        yield n, rec


def _blank(rec: list[str]) -> bool:
    return not any(c.strip() for c in rec)


def read_csv(data: bytes, filename: str | None = None, *, header_row: int = 1,
             delimiter: str | None = None, decimal_separator: str = "auto",
             date_order: str = "auto", row_offset: int = 0, row_limit: int = 50,
             tail_rows: int = 5, max_rows: int = 1_000_000, max_cols: int = 2_000,
             max_bytes: int = DEFAULT_MAX_BYTES) -> dict[str, Any]:
    """Read a CSV attachment.

    header_row         1-based record number of the header; 0 = no header.
                       Records above it are returned as ``preamble``.
    delimiter          None = sniff from , ; TAB |
    decimal_separator  'auto' (per-column evidence) | '.' | ','
    date_order         'auto' (per-column evidence) | 'DMY' | 'MDY'
    row_offset         0-based index into non-blank data rows for the page
    row_limit          page size, 0..5000 (0 = profile only); default 50 is a
                       sample for pattern recognition, not the whole file
    tail_rows          last N data rows returned separately, 0..50
    """
    params = {"header_row": header_row, "delimiter": delimiter,
              "decimal_separator": decimal_separator, "date_order": date_order,
              "row_offset": row_offset, "row_limit": row_limit, "tail_rows": tail_rows,
              "max_rows": max_rows, "max_cols": max_cols}
    if header_row < 0:
        raise ToolError(PARAM_INVALID, "header_row must be >= 0")
    if decimal_separator not in ("auto", ".", ","):
        raise ToolError(PARAM_INVALID, "decimal_separator must be auto, '.' or ','")
    if date_order not in ("auto", "DMY", "MDY"):
        raise ToolError(PARAM_INVALID, "date_order must be auto, DMY or MDY")
    if delimiter is not None and len(delimiter) != 1:
        raise ToolError(PARAM_INVALID, "delimiter must be one character")
    if row_offset < 0 or not 0 <= row_limit <= MAX_PAGE or not 0 <= tail_rows <= 50:
        raise ToolError(PARAM_INVALID,
                        f"row_offset >= 0, row_limit 0..{MAX_PAGE}, tail_rows 0..50")
    check_size(data, max_bytes)

    text, encoding = _decode(data)
    dialect = _dialect(text, delimiter)

    # ---- pass 1: shape, header, preamble and column evidence (counters only)
    header: list[str] = []
    preamble: list[dict[str, Any]] = []
    width = header_width = records = data_rows = blank_rows = ragged = 0
    dec_votes: dict[int, Counter] = {}
    date_votes: dict[int, Counter] = {}
    for n, rec in _records(text, dialect):
        records = n
        if n > max_rows:
            raise ToolError(INPUT_TOO_LARGE, f"more than {max_rows} records",
                            detail={"limit": max_rows})
        width = max(width, len(rec))
        if width > max_cols:
            raise ToolError(INPUT_TOO_LARGE, f"{width} columns, limit {max_cols}",
                            detail={"columns": width, "limit": max_cols})
        if header_row and n < header_row:
            preamble.append({"row": n, "values": rec})
            continue
        if header_row and n == header_row:
            header, header_width = rec, len(rec)
            continue
        if _blank(rec):
            blank_rows += 1
            continue
        data_rows += 1
        if header_row and len(rec) != header_width:
            ragged += 1
        for c, raw in enumerate(rec):
            if not raw.strip():
                continue
            if decimal_separator == "auto" and (v := decimal_sep_vote(raw)):
                dec_votes.setdefault(c, Counter())[v] += 1
            if date_order == "auto" and (v := date_order_vote(raw)):
                date_votes.setdefault(c, Counter())[v] += 1
    if header_row and header_row > records:
        raise ToolError(PARAM_INVALID, f"header_row {header_row} beyond last record {records}")

    col_dec: dict[int, tuple[str, str]] = {}
    col_date: dict[int, tuple[str | None, str]] = {}
    for c in range(width):
        if decimal_separator == "auto":
            col_dec[c] = resolve_votes(sorted(dec_votes.get(c, Counter()).elements()), ".")
        else:
            col_dec[c] = (decimal_separator, "param")
        if date_order == "auto":
            choice, ev = resolve_votes(sorted(date_votes.get(c, Counter()).elements()), "")
            col_date[c] = (choice or None, ev) if ev == "inferred" else (None, ev)
        else:
            col_date[c] = (date_order, "param")

    # ---- pass 2: type every cell for the counts; keep only page + tail
    counts: list[Counter] = [Counter() for _ in range(width)]
    page: list[dict[str, Any]] = []
    tail: deque = deque(maxlen=tail_rows)
    idx = -1
    for n, rec in _records(text, dialect):
        if (header_row and n <= header_row) or _blank(rec):
            continue
        idx += 1
        cells = []
        for c, raw in enumerate(rec):
            if not raw.strip():
                continue
            typed = type_text(raw, decimal_sep=col_dec[c][0], date_order=col_date[c][0])
            counts[c][typed["type"]] += 1
            cells.append({"ref": a1(n, c + 1), "raw": raw, **typed})
        row = {"row": n, "index": idx, "cells": cells}
        if row_offset <= idx < row_offset + row_limit:
            page.append(row)
        if tail_rows:
            tail.append(row)

    page_end = row_offset + len(page)
    tail_out = [r for r in tail if not row_offset <= r["index"] < page_end]

    columns = []
    for c in range(width):
        tc = dict(sorted(counts[c].items()))
        columns.append({
            "index": c + 1,
            "letter": col_letter(c + 1),
            "name": header[c] if c < len(header) else "",
            "inferred_type": column_type(tc),
            "type_counts": tc,
            "non_empty": sum(tc.values()),
            "decimal_separator": col_dec[c][0],
            "decimal_separator_evidence": col_dec[c][1],
            "date_order": col_date[c][0],
            "date_order_evidence": col_date[c][1],
        })

    result = {
        "encoding": encoding,
        "delimiter": dialect.delimiter,
        "quotechar": dialect.quotechar,
        "header_row": header_row or None,
        "record_count": records,
        "data_row_count": data_rows,
        "blank_rows_skipped": blank_rows,
        "ragged_rows": ragged,
        "columns": columns,
        "preamble": preamble,
        "page": {"row_offset": row_offset, "row_limit": row_limit, "returned": len(page),
                 "has_more": page_end < data_rows,
                 "next_offset": page_end if page_end < data_rows else None},
        "rows": page,
        "tail": tail_out,
    }
    return envelope(TOOL, data, filename, params, result)
