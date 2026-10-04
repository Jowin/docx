"""Shape an extraction result for the writer tools (CSV, Excel, Word, PDF).

The writers accept what the runtime returns, in either form:

* the plain form: a list of records (``[{"invoice_number": ..., ...}, ...]``);
* the extended form: ``{"data": [...], "records": [...], "flags": [...], "metadata": {...}}``.

``shape(result)`` turns either into one view every writer renders from:

* ``columns``: the scalar fields, in the order the records give them;
* ``rows``: one per record, scalar values only;
* ``arrays``: per array field (line items), its sub-columns and its rows,
  each tagged with the index (1-based) of the record it belongs to;
* ``meta``: per record, its confidence, flags and per-field sources and
  confidences (extended form only), plus the run's flags and metadata.

Pure, like the reader tools: no clock, no filesystem, no network.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from .common import PARAM_INVALID, ToolError

#: A spreadsheet cell starting with one of these runs as a formula (CSV / formula injection).
_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


@dataclass
class RecordMeta:
    confidence: float | None = None
    flagged: bool | None = None
    flags: list[str] = field(default_factory=list)
    fields: dict[str, dict[str, Any]] = field(default_factory=dict)   # name -> {confidence, source}


@dataclass
class Shaped:
    columns: list[str]
    rows: list[dict[str, Any]]
    arrays: dict[str, tuple[list[str], list[dict[str, Any]]]]
    meta: list[RecordMeta]
    extended: bool
    run_flags: list[str]
    flagged: bool | None
    confidence: float | None
    metadata: dict[str, Any]

    @property
    def title(self) -> str:
        cfg = self.metadata.get("config") or {}
        inp = self.metadata.get("input") or {}
        what = f"{cfg.get('client')}/{cfg.get('usecase')}" if cfg.get("client") else "Extraction result"
        return f"{what}: {inp.get('name')}" if inp.get("name") else what


def shape(result: Any) -> Shaped:
    if isinstance(result, list):
        data, records, ext = result, [], {}
    elif isinstance(result, dict) and isinstance(result.get("data"), list):
        data, records, ext = result["data"], result.get("records") or [], result
    else:
        raise ToolError(PARAM_INVALID, "result must be a list of records or an extraction result with 'data'")
    if not all(isinstance(r, dict) for r in data):
        raise ToolError(PARAM_INVALID, "every record must be an object")

    columns: list[str] = []
    array_cols: dict[str, list[str]] = {}
    for rec in data:
        for name, value in rec.items():
            if _is_rows(value):
                cols = array_cols.setdefault(name, [])
                for item in value:
                    for k in item:
                        if k not in cols:
                            cols.append(k)
            elif name not in columns and name not in array_cols:
                columns.append(name)
    rows = [{c: rec.get(c) for c in columns} for rec in data]
    arrays: dict[str, tuple[list[str], list[dict[str, Any]]]] = {}
    for name, cols in array_cols.items():
        items = []
        for i, rec in enumerate(data, start=1):
            for item in rec.get(name) or []:
                items.append({"_record": i, **{c: item.get(c) for c in cols}})
        arrays[name] = (cols, items)

    meta = []
    for i in range(len(data)):
        r = records[i] if i < len(records) and isinstance(records[i], dict) else {}
        fields = {n: {"confidence": f.get("confidence"), "source": f.get("source")}
                  for n, f in (r.get("fields") or {}).items() if isinstance(f, dict)}
        meta.append(RecordMeta(confidence=r.get("confidence"), flagged=r.get("flagged"),
                               flags=list(r.get("flags") or []), fields=fields))
    return Shaped(columns=columns, rows=rows, arrays=arrays, meta=meta, extended=bool(ext),
                  run_flags=list(ext.get("flags") or []), flagged=ext.get("flagged"),
                  confidence=ext.get("confidence"), metadata=ext.get("metadata") or {})


def _is_rows(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(x, dict) for x in value) and bool(value)


def cell(value: Any) -> Any:
    """A value as a spreadsheet cell: numbers stay numbers, everything else is text."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float, Decimal)):
        return value
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


def safe_text(value: Any) -> Any:
    """Neutralise text a spreadsheet would run as a formula ('=HYPERLINK(...)'), keep numbers."""
    v = cell(value)
    if isinstance(v, str) and v.startswith(_FORMULA_START):
        try:
            float(v.replace(",", ""))           # "-12.50" is a number written as text: leave it
            return v
        except ValueError:
            return "'" + v
    return v


def text(value: Any) -> str:
    """A value for a printed report."""
    v = cell(value)
    if v is None:
        return "—"
    if isinstance(v, bool):
        return "yes" if v else "no"
    return str(v)
