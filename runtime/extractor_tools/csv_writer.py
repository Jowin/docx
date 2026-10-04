"""CSV Writer tool: an extraction result -> CSV bytes.

One row per record, columns in the order the record gives its fields. When
the records carry exactly one array field (an invoice's line items), the CSV
is *exploded* by default: one row per item, the record's fields repeated, the
item's columns named ``<array>.<column>``; a record with no items still gets
its row. ``explode=""`` keeps one row per record and writes arrays as JSON.

Extended results add ``_confidence``, ``_flagged`` and ``_flags`` (record
level) unless ``include_meta`` is false. Text a spreadsheet would run as a
formula is prefixed with ``'``. UTF-8 with a byte-order mark by default, so
Excel opens accented and currency characters correctly.

Pure: the same result gives byte-identical output.
"""
from __future__ import annotations

import csv
import io
from typing import Any

from .common import PARAM_INVALID, ToolError
from .result_table import cell, safe_text, shape

TOOL = "csv_writer"
MEDIA_TYPE = "text/csv"
EXTENSION = "csv"


def write_csv(result: Any, *, explode: str | None = None, include_meta: bool = True,
              delimiter: str = ",", bom: bool = True) -> bytes:
    if delimiter not in (",", ";", "\t", "|"):
        raise ToolError(PARAM_INVALID, "delimiter must be one of , ; tab |")
    s = shape(result)
    if explode is None:
        explode = next(iter(s.arrays)) if len(s.arrays) == 1 else ""
    if explode and explode not in s.arrays:
        explode = ""                                  # nothing to explode: plain rows
    item_cols = s.arrays[explode][0] if explode else []
    other_arrays = [a for a in s.arrays if a != explode]
    meta_cols = ["_confidence", "_flagged", "_flags"] if (include_meta and s.extended) else []
    header = ["_record", *s.columns, *other_arrays, *[f"{explode}.{c}" for c in item_cols], *meta_cols]

    buf = io.StringIO()
    w = csv.writer(buf, delimiter=delimiter, lineterminator="\r\n", quoting=csv.QUOTE_MINIMAL)
    w.writerow(header)
    for i, row in enumerate(s.rows, start=1):
        m = s.meta[i - 1]
        base = [i, *[safe_text(row[c]) for c in s.columns]]
        base += [cell(_array_value(result, i - 1, a)) for a in other_arrays]
        tail = [m.confidence, "" if m.flagged is None else str(m.flagged).lower(), "; ".join(m.flags)] \
            if meta_cols else []
        items = [it for it in (s.arrays[explode][1] if explode else []) if it["_record"] == i]
        if not items:
            w.writerow([_csv(v) for v in base + [None] * len(item_cols) + tail])
        for it in items:
            w.writerow([_csv(v) for v in base + [safe_text(it.get(c)) for c in item_cols] + tail])
    text = buf.getvalue()
    return (b"\xef\xbb\xbf" if bom else b"") + text.encode("utf-8")


def _array_value(result: Any, index: int, name: str) -> Any:
    data = result if isinstance(result, list) else result.get("data") or []
    return data[index].get(name) if index < len(data) else None


def _csv(v: Any) -> Any:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    return v
