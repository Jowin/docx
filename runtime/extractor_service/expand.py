"""Read every row of a large blotter: the model maps the columns, code reads the rows.

The evidence the extractor sees is a slice of each table (``evidence.rows``
from the top, ``evidence.tail_rows`` from the bottom). For a dictionary whose
records are table rows (``"record_key": "@row"``), the slice is not the
answer: a 5,000-row settlement blotter is 5,000 records. So after the
extractor (model or stub) has answered from the slice, this step:

1. **Learns the mapping from the answer.** Every value the extractor returned
   cites a cell (``d2#Sheet1!E7``); the column each field was cited from, by
   majority over the sampled rows, is that field's column. A field with no
   cited column falls back to a header that is exactly one of its labels.
   Fields the extractor filled from outside the table ("Portfolio: GLB-EQ-01"
   in the email body) and that were the same for every sampled row are
   *constants* for the rest of the rows.
2. **Streams every row the extractor did not answer for** with the same reader tools that made the
   evidence (``stream_csv_rows`` with the profiled delimiter, decimal mark and
   date order per column; ``stream_sheet_rows`` for xlsx/xls), one row at a
   time, and builds each row's record from its cells.
3. **Checks every value as it reads it.** Values come straight from the cited
   cell, so they are grounded by construction; each is normalised and
   validated against its field like any extracted value. A required field the
   row lacks is ``missing_field``; a value that breaks its type is
   ``schema_validation_failed`` on that record only.

Totals and blank rows are skipped, like the stub does. The number of records
is capped by ``limits.max_records`` (default 25,000; ``records_truncated``
beyond it) and the work by the run ceiling. Files the step does not read in
full keep their ``rows_truncated`` note, and ``verify`` flags them
``content_truncated:<file>`` so nothing is cut silently.
"""
from __future__ import annotations

import re
import statistics
import time
from collections import Counter, defaultdict
from typing import Any

from .evidence import Doc, Table
from .schema import ROW_KEY, normalize

DEFAULT_MAX_RECORDS = 25_000
#: A row whose first text says so is a total, not an instruction.
TOTAL_ROW = re.compile(r"^\s*(sub-?\s?total|grand\s+total|totals?)\b", re.I)
_LOC = re.compile(r"^(?:(?P<sheet>.+)!)?(?P<col>[A-Z]{1,3})(?P<row>\d+)$")


def _parse_loc(loc: str) -> tuple[str | None, int, int] | None:
    from extractor_tools.common import col_index
    m = _LOC.match(loc or "")
    if not m:
        return None
    return m.group("sheet"), int(m.group("row")), col_index(m.group("col"))


def _sheet_of(t: Table) -> str | None:
    return t.meta.get("sheet") if t.meta.get("kind") == "sheet" else None


def _expandable(doc: Doc) -> list[Table]:
    """Per CSV / sheet, the table that runs into the part of the file the evidence did not hold."""
    if doc.status != "read" or doc.kind not in ("csv", "excel"):
        return []
    truncated = [n for n in doc.notes if n.startswith("rows_truncated:")]
    if not truncated:
        return []
    out = []
    by_group: dict[str, list[Table]] = defaultdict(list)
    for t in doc.tables:
        if t.meta.get("kind") in ("csv", "sheet") and t.header:
            by_group[t.group].append(t)
    for group, tables in by_group.items():
        if doc.kind == "excel" and not any(n.startswith(f"rows_truncated:{group}:") for n in truncated):
            continue
        last = max(tables, key=lambda t: max((b.row or 0 for row in t.rows for b in row), default=0))
        out.append(last)
    return out


def _mapping(cfg: Any, doc: Doc, table: Table, raw_records: list[dict[str, Any]]):
    """Field -> column from the extractor's citations (majority), then exact header labels."""
    sheet = _sheet_of(table)
    rows_in_table = {b.row for row in table.rows for b in row if b.row}
    votes: dict[str, Counter] = defaultdict(Counter)
    confs: dict[str, list[float]] = defaultdict(list)
    inside: list[dict[str, Any]] = []
    for rec in raw_records:
        hits = 0
        for name, v in rec.items():
            if not isinstance(v, dict) or v.get("value") in (None, ""):
                continue
            doc_id, _, loc = str(v.get("source") or "").partition("#")
            p = _parse_loc(loc) if doc_id == doc.doc_id else None
            if p and p[0] == sheet and p[1] in rows_in_table:
                votes[name][p[2]] += 1
                confs[name].append(float(v.get("confidence") or 0.0))
                hits += 1
        if hits:
            inside.append(rec)
    header = {b.col: " ".join(b.text.split()).casefold() for b in table.header}
    mapping: dict[str, int] = {}
    for f in cfg.dictionary.fields:
        if f.type == "array":
            continue
        if votes.get(f.name):
            col, n = votes[f.name].most_common(1)[0]
            if col in header:
                mapping[f.name] = col
                continue
        labels = {lab.casefold() for lab in f.labels}
        col = next((c for c, text in header.items() if text in labels and c not in mapping.values()), None)
        if col is not None:
            mapping[f.name] = col
    # constants: same value, cited outside this table, for every sampled row of it
    constants: dict[str, dict[str, Any]] = {}
    for f in cfg.dictionary.fields:
        if f.type == "array" or f.name in mapping or not inside:
            continue
        seen = [rec.get(f.name) for rec in inside]
        vals = {str(v.get("value")) for v in seen if isinstance(v, dict) and v.get("value") not in (None, "")}
        if len(vals) == 1 and all(isinstance(v, dict) and v.get("value") not in (None, "") for v in seen):
            constants[f.name] = dict(seen[0])
    field_conf = {name: round(statistics.median(c), 4) if c else 0.85 for name, c in confs.items()}
    return mapping, constants, field_conf


def _answered_rows(doc: Doc, table: Table, raw_records: list[dict[str, Any]]) -> set[int]:
    """Rows of this table the extractor already returned a record for (by its citations).

    Every other data row is read here, including evidence rows the extractor
    left out (a model that answered for the first rows only)."""
    sheet = _sheet_of(table)
    rows: set[int] = set()
    for rec in raw_records:
        for v in rec.values():
            if not isinstance(v, dict):
                continue
            doc_id, _, loc = str(v.get("source") or "").partition("#")
            p = _parse_loc(loc) if doc_id == doc.doc_id else None
            if p and p[0] == sheet:
                rows.add(p[1])
    return rows


def _row_source(doc: Doc, table: Table, ref: str) -> str:
    sheet = _sheet_of(table)
    return f"{doc.doc_id}#{sheet}!{ref}" if sheet else f"{doc.doc_id}#{ref}"


def _stream(table: Table, data: bytes, max_bytes: int):
    meta = table.meta
    if meta["kind"] == "csv":
        from extractor_tools.csv_reader import stream_csv_rows
        return stream_csv_rows(
            data, header_row=meta["header_row"], delimiter=meta.get("delimiter"),
            decimal_separators={int(k): v for k, v in (meta.get("decimal_separators") or {}).items()},
            date_orders={int(k): v for k, v in (meta.get("date_orders") or {}).items()}, max_bytes=max_bytes)
    from extractor_tools.spreadsheet_reader import stream_sheet_rows
    return stream_sheet_rows(data, sheet=meta["sheet"], start_row=(meta.get("header_row") or 0) + 1,
                             max_bytes=max_bytes)


def expand(cfg: Any, docs: list[Doc], items: list[Any], raw_records: list[dict[str, Any]], *,
           spool: Any = None, deadline: float | None = None) -> dict[str, Any]:
    """Records for every table row the evidence did not hold. See the module docstring."""
    if cfg.dictionary.record_key != ROW_KEY:
        return {"records": [], "reports": [], "reasons": []}
    from .evidence import _max_bytes
    max_records = int(cfg.limits.get("max_records") or DEFAULT_MAX_RECORDS)
    by_order = {getattr(i, "order", None): i for i in items}
    fields = {f.name: f for f in cfg.dictionary.fields if f.type != "array"}
    out: list[dict[str, Any]] = []
    reports, reasons = [], []
    budget = max_records - len(raw_records)
    for doc in docs:
        for table in _expandable(doc):
            mapping, constants, field_conf = _mapping(cfg, doc, table, raw_records)
            report = {"document": doc.source, "table": table.group,
                      "mapping": {n: next((b.text for b in table.header if b.col == c), "") for n, c in mapping.items()},
                      "constants": {n: v.get("value") for n, v in constants.items()},
                      "rows_read": 0, "records": 0, "complete": False}
            reports.append(report)
            if len(mapping) < 2:
                report["skipped"] = "fewer than two columns map to fields"
                continue
            item = by_order.get(doc.order)
            if item is None:
                report["skipped"] = "source bytes unavailable"
                continue
            present = _answered_rows(doc, table, raw_records)
            col_to_field = {c: n for n, c in mapping.items()}
            stopped = None
            for row in _stream(table, item.read(spool), _max_bytes(cfg.evidence)):
                if row["row"] in present:
                    continue
                report["rows_read"] += 1
                if deadline and report["rows_read"] % 500 == 0 and time.time() > deadline:
                    stopped = "agent_timeout:expand"
                    break
                cells = {int(_parse_loc(c["ref"])[2]): c for c in row["cells"] if _parse_loc(c["ref"])}
                texts = [str(c.get("raw", c.get("value", ""))) for c in row["cells"] if c.get("type") == "string"]
                if texts and TOTAL_ROW.match(texts[0]):
                    continue
                mapped = [c for col, c in cells.items() if col in col_to_field
                          and str(c.get("raw", c.get("value", ""))).strip()]
                if len(mapped) < 2:
                    continue
                if budget <= 0:
                    stopped = f"records_truncated:{max_records}"
                    break
                rec, rec_reasons = _record(cfg, fields, doc, table, cells, mapping, constants, field_conf)
                out.append({"fields": rec, "reasons": rec_reasons, "_row": row["row"], "_doc": doc.doc_id})
                report["records"] += 1
                budget -= 1
            if stopped:
                reasons.append(stopped)
                report["stopped"] = stopped
            else:
                report["complete"] = True
    return {"records": out, "reports": reports, "reasons": reasons}


def _record(cfg, fields, doc, table, cells, mapping, constants, field_conf):
    rec: dict[str, dict[str, Any]] = {}
    reasons: list[str] = []
    for name, f in fields.items():
        entry: dict[str, Any] = {"value": None, "confidence": 0.0, "source": None}
        if name in mapping and mapping[name] in cells:
            c = cells[mapping[name]]
            raw = c.get("value") if c.get("value") is not None else c.get("raw")
            value, err = normalize(f, raw, date_order=cfg.date_order)
            if value is not None:
                conf = field_conf.get(name, 0.85) * (0.5 if err else 1.0)
                entry.update(value=value, confidence=round(conf, 4), source=_row_source(doc, table, c["ref"]),
                             grounding="verified", **({"error": err} if err else {}))
                if err:
                    reasons.append(f"schema_validation_failed:{name}")
        elif name in constants:
            v = constants[name]
            value, err = normalize(f, v.get("value"), date_order=cfg.date_order)
            if value is not None:
                entry.update(value=value, confidence=round(float(v.get("confidence") or 0.8) * 0.95, 4),
                             source=v.get("source"), grounding="verified")
        if entry["value"] is None and f.required:
            reasons.append(f"missing_field:{name}")
        rec[name] = entry
    return rec, reasons
