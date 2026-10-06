"""Read every row of a large blotter: the model maps the columns, code reads the rows.

The evidence the extractor sees is a slice of each document (``evidence.rows``
and ``evidence.tail_rows`` of a CSV or sheet; ``evidence.pages`` and
``evidence.tail_pages`` of a PDF). For a dictionary whose records are table
rows (``"record_key": "@row"``), the slice is not the answer: a 5,000-row
settlement blotter, or a 500-page settlement report, is thousands of records.
So after the extractor (model or stub) has answered from the slice, this step:

1. **Learns the mapping from the answer.** Every value the extractor returned
   cites a cell (``d2#Sheet1!E7``, ``d1#p3:T1:R4C7``, ``d1#p9:OT1:R2C3`` for a
   table Textract read off a scanned page); the column each field was cited
   from, by majority, is that field's column, and its header text is
   remembered. A field with no cited column falls back to a header that is
   exactly one of its labels. Fields the extractor filled from outside the
   table ("Portfolio: GLB-EQ-01" in the email body) and that were the same for
   every sampled row are *constants* for the rest of the rows.
2. **Reads every row the extractor did not answer for**, with the same reader
   tools that made the evidence: ``stream_csv_rows`` / ``stream_sheet_rows``
   for CSV and Excel; for a PDF, every page's tables (pdfplumber), and the
   pages with no text layer through OCR (Textract tables; see ocr.py). On a
   PDF page the columns are found by header text when the table repeats its
   header, else by position (a continuation table without one).
3. **Checks every value as it reads it.** Values come straight from a cell,
   so they are grounded by construction; each is normalised and validated
   against its field like any extracted value. A required field the row lacks
   is ``missing_field``; a value that breaks its type is
   ``schema_validation_failed`` on that record only. OCR'd cells carry the OCR
   engine's confidence.

Each document is read in the parsing sandbox (sandbox.py): its own process,
memory cap and time budget (what is left of ``limits.run_ceiling_s``).
Totals and blank rows are skipped. Records are capped by
``limits.max_records`` (default 25,000; ``records_truncated`` beyond it).
Content no step read in full is flagged ``content_truncated:<file>`` by
``verify``, so nothing is cut silently.
"""
from __future__ import annotations

import re
import statistics
import time
from collections import Counter, defaultdict
from typing import Any, Iterator

from .evidence import Doc, Table
from .schema import ROW_KEY, normalize

DEFAULT_MAX_RECORDS = 25_000
PDF_WINDOW = 50
#: A row whose first text says so is a total, not an instruction.
TOTAL_ROW = re.compile(r"^\s*(sub-?\s?total|grand\s+total|totals?)\b", re.I)
_CELL = re.compile(r"^(?:(?P<sheet>.+)!)?(?P<col>[A-Z]{1,3})(?P<row>\d+)$")
_PDF = re.compile(r"^(?P<group>p(?P<page>\d+):(?P<ocr>O?)T(?P<t>\d+)):R(?P<row>\d+)C(?P<col>\d+)$")


def parse_loc(loc: str) -> tuple[str | None, int, int, tuple] | None:
    """(group, row, col, sort key) for a table cell locator; None for anything else.

    group: the sheet name (None for a CSV), or the PDF table (``p3:T1``, ``p9:OT1``).
    """
    from extractor_tools.common import col_index
    m = _PDF.match(loc or "")
    if m:
        page, t, row = int(m.group("page")), int(m.group("t")), int(m.group("row"))
        return m.group("group"), row, int(m.group("col")), ("", page, (1 if m.group("ocr") else 0), t, row)
    m = _CELL.match(loc or "")
    if m:
        row = int(m.group("row"))
        return m.group("sheet"), row, col_index(m.group("col")), (m.group("sheet") or "", 0, 0, 0, row)
    return None




def _norm(text: str) -> str:
    return " ".join(str(text or "").split()).casefold()


def _group_of(t: Table) -> str | None:
    kind = t.meta.get("kind")
    if kind == "sheet":
        return t.meta.get("sheet")
    if kind == "pdf":
        return t.group
    return None


# ---------------------------------------------------------------------- what to read


def _expandable(doc: Doc) -> list[Table]:
    """The sample tables of a document whose rest the evidence did not hold."""
    if doc.status != "read":
        return []
    if doc.kind == "pdf":
        if not any(n.startswith("pages_truncated:") for n in doc.notes):
            return []
        return [t for t in doc.tables if t.meta.get("kind") == "pdf" and t.header]
    if doc.kind not in ("csv", "excel"):
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
        out.append(max(tables, key=lambda t: max((b.row or 0 for row in t.rows for b in row), default=0)))
    return out


def _mapping(cfg: Any, doc: Doc, tables: list[Table], raw_records: list[dict[str, Any]]):
    """Field -> column (and its header text) from the extractor's citations, then exact header labels."""
    groups = {_group_of(t): t for t in tables}
    votes: dict[str, Counter] = defaultdict(Counter)
    headers: dict[str, Counter] = defaultdict(Counter)
    confs: dict[str, list[float]] = defaultdict(list)
    inside: list[dict[str, Any]] = []
    for rec in raw_records:
        hits = 0
        for name, v in rec.items():
            if not isinstance(v, dict) or v.get("value") in (None, ""):
                continue
            doc_id, _, loc = str(v.get("source") or "").partition("#")
            p = parse_loc(loc) if doc_id == doc.doc_id else None
            if p and p[0] in groups:
                t = groups[p[0]]
                votes[name][p[2]] += 1
                head = next((b.text for b in t.header if b.col == p[2]), "")
                if head:
                    headers[name][_norm(head)] += 1
                confs[name].append(float(v.get("confidence") or 0.0))
                hits += 1
        if hits:
            inside.append(rec)
    sample = max(tables, key=lambda t: len(t.rows))
    header = {b.col: _norm(b.text) for b in sample.header}
    mapping: dict[str, int] = {}
    head_text: dict[str, str] = {}
    for f in cfg.dictionary.fields:
        if f.type == "array":
            continue
        if votes.get(f.name):
            mapping[f.name] = votes[f.name].most_common(1)[0][0]
            if headers.get(f.name):
                head_text[f.name] = headers[f.name].most_common(1)[0][0]
            continue
        labels = {_norm(lab) for lab in f.labels}
        col = next((c for c, text in header.items() if text in labels and c not in mapping.values()), None)
        if col is not None:
            mapping[f.name], head_text[f.name] = col, header[col]
    constants: dict[str, dict[str, Any]] = {}
    for f in cfg.dictionary.fields:
        if f.type == "array" or f.name in mapping or not inside:
            continue
        seen = [rec.get(f.name) for rec in inside]
        vals = {str(v.get("value")) for v in seen if isinstance(v, dict) and v.get("value") not in (None, "")}
        if len(vals) == 1 and all(isinstance(v, dict) and v.get("value") not in (None, "") for v in seen):
            constants[f.name] = dict(seen[0])
    field_conf = {name: round(statistics.median(c), 4) if c else 0.85 for name, c in confs.items()}
    return mapping, head_text, constants, field_conf


def _answered(doc: Doc, raw_records: list[dict[str, Any]]) -> set[tuple[str | None, int]]:
    """(group, row) of every table row the extractor already returned a record for."""
    rows: set[tuple[str | None, int]] = set()
    for rec in raw_records:
        for v in rec.values():
            if not isinstance(v, dict):
                continue
            doc_id, _, loc = str(v.get("source") or "").partition("#")
            p = parse_loc(loc) if doc_id == doc.doc_id else None
            if p:
                rows.add((p[0], p[1]))
    return rows


# ---------------------------------------------------------------------- reading rows

# A row: (group, row number, {col: cell}), cell = {"ref", "raw", "value", "type", "confidence"?}


def _tabular_rows(table: Table, data: bytes, max_bytes: int) -> Iterator[tuple]:
    meta = table.meta
    if meta["kind"] == "csv":
        from extractor_tools.csv_reader import stream_csv_rows
        rows = stream_csv_rows(
            data, header_row=meta["header_row"], delimiter=meta.get("delimiter"),
            decimal_separators={int(k): v for k, v in (meta.get("decimal_separators") or {}).items()},
            date_orders={int(k): v for k, v in (meta.get("date_orders") or {}).items()}, max_bytes=max_bytes)
        group = None
    else:
        from extractor_tools.spreadsheet_reader import stream_sheet_rows
        rows = stream_sheet_rows(data, sheet=meta["sheet"], start_row=(meta.get("header_row") or 0) + 1,
                                 max_bytes=max_bytes)
        group = meta["sheet"]
    for row in rows:
        cells = {}
        for c in row["cells"]:
            p = parse_loc(c["ref"])
            if p:
                cells[p[2]] = {**c, "ref": f"{group}!{c['ref']}" if group else c["ref"]}
        yield group, row["row"], cells


def _grid_of(table: Table) -> list[list[dict[str, Any]]]:
    """A table already in the evidence (a page the extractor saw), header row included, as cells."""
    return [[{"ref": b.locator, "raw": b.text, "value": None, "type": "string", "col": b.col, "row": b.row,
              **({"confidence": b.confidence} if b.confidence is not None else {})} for b in row if b.text]
            for row in [table.header] + table.rows]


def _pdf_table_rows(group: str, grid: list[list[dict[str, Any]]], mapping, head_text, width: int,
                    notes: list[str]) -> Iterator[tuple]:
    """One PDF table: find its columns by header text, or by position when it is a continuation
    as wide as the sample table; any other table (a summary, a totals box) is not settlement rows."""
    if not grid or not grid[0]:
        return
    first = {c["col"]: _norm(c["raw"]) for c in grid[0]}
    by_text = {text: col for col, text in first.items()}
    hits = {f: by_text[h] for f, h in head_text.items() if h in by_text}
    if len(hits) >= 2 or (hits and len(hits) == len(head_text)):
        cols = {f: hits.get(f, mapping[f]) for f in mapping}
        body = grid[1:]
    elif max((c["col"] for row in grid for c in row), default=0) == width:
        cols, body = dict(mapping), grid                      # a continuation without its header
    else:
        notes.append(f"table_not_rows:{group}")
        return
    remap = {col: f for f, col in cols.items()}
    for row in body:
        if not row:
            continue
        cells = {}
        for c in row:
            f = remap.get(c["col"])
            if f is not None:
                cells[mapping[f]] = c                         # keyed by the sample's column for _record
        extra = {c["col"]: c for c in row if c["col"] not in remap}
        yield group, row[0]["row"], {**{k + 10_000: v for k, v in extra.items()}, **cells}


def _pdf_rows(doc: Doc, data: bytes, mapping, head_text, width: int, ev: dict[str, Any],
              notes: list[str]) -> Iterator[tuple]:
    from extractor_tools.pdf_text import extract_pdf_text
    from . import ocr
    from .evidence import _max_bytes
    evidence_pages = {int(b.group[1:].split(":")[0]) for b in doc.blocks
                      if b.group.startswith("p") and b.group[1:].split(":")[0].isdigit()}
    for t in doc.tables:                                      # the pages the extractor saw
        if t.meta.get("kind") == "pdf":
            yield from _pdf_table_rows(t.group, _grid_of(t), mapping, head_text, width, notes)
    n_pages = next((int(n.split(":")[1]) for n in doc.notes if n.startswith("pages_truncated:")), 0)
    blank: list[int] = []
    start = 1
    while start <= n_pages:
        r = extract_pdf_text(data, doc.name, start_page=start, page_limit=PDF_WINDOW, tail_pages=0,
                             include_amounts=False, max_bytes=_max_bytes(ev))["result"]
        no_text = set(r["pages_without_text_layer"])
        for page in r["pages"]:
            p = page["page"]
            if p in evidence_pages:
                continue
            if page.get("error"):
                notes.append(f"page_unreadable:p{p}")
                continue
            if p in no_text:
                blank.append(p)
                continue
            for t in page.get("tables", []):
                grid = [[{"ref": c["locator"], "raw": (c["text"] or "").strip(), "value": None, "type": "string",
                          "col": int(c["locator"].rsplit("C", 1)[1]), "row": ri}
                         for c in row if (c["text"] or "").strip()]
                        for ri, row in enumerate(t["rows"], start=1)]
                yield from _pdf_table_rows(t["locator"], grid, mapping, head_text, width, notes)
        start += PDF_WINDOW
    if blank:
        if not (ev.get("ocr", True) and ocr.available()):
            notes.append("pages_unread:no_ocr:" + ",".join(f"p{p}" for p in blank[:20]))
            return
        try:
            res = ocr.ocr_pdf(data, blank, timeout_s=float(ev.get("ocr_timeout_s", 60)))
        except ocr.OcrUnavailable as exc:
            notes.append(f"ocr_unavailable:{exc}"[:200])
            return
        notes.extend(res.notes)
        for p, why in sorted(res.failed.items()):
            notes.append(f"ocr_failed:p{p}:{why}"[:200])
        for p, page in sorted(res.pages.items()):
            if not page.tables:
                notes.append(f"ocr_no_tables:p{p}")
            for ti, table in enumerate(page.tables, start=1):
                group = f"p{p}:OT{ti}"
                grid = [[{"ref": f"{group}:R{ri}C{ci}", "raw": text.strip(), "value": None, "type": "string",
                          "col": ci, "row": ri, "confidence": conf}
                         for ci, (text, conf) in enumerate(row, start=1) if text.strip()]
                        for ri, row in enumerate(table.rows, start=1)]
                yield from _pdf_table_rows(group, grid, mapping, head_text, width, notes)


# ---------------------------------------------------------------------- one document


def expand_doc(cfg: Any, doc: Doc, tables: list[Table], data: bytes, raw_records: list[dict[str, Any]],
               budget: int, deadline: float | None) -> dict[str, Any]:
    """Every unanswered row of one document as records. Runs inside the sandbox."""
    from .evidence import _max_bytes
    fields = {f.name: f for f in cfg.dictionary.fields if f.type != "array"}
    mapping, head_text, constants, field_conf = _mapping(cfg, doc, tables, raw_records)
    sample = max(tables, key=lambda t: len(t.rows))
    report: dict[str, Any] = {
        "document": doc.source,
        "table": sample.group if doc.kind != "pdf" else f"{len(tables)} sampled table(s)",
        "mapping": {n: next((b.text for t in tables for b in t.header
                             if (_norm(b.text) == head_text[n] if n in head_text else b.col == c)), "")
                    for n, c in mapping.items()},
        "constants": {n: v.get("value") for n, v in constants.items()},
        "rows_read": 0, "records": 0, "complete": False}
    if len(mapping) < 2:
        report["skipped"] = "fewer than two columns map to fields"
        return {"records": [], "report": report, "reasons": [], "notes": []}
    answered = _answered(doc, raw_records)
    col_to_field = {c: n for n, c in mapping.items()}
    notes: list[str] = []
    out: list[dict[str, Any]] = []
    stopped = None
    width = max((b.col or 0 for b in sample.header), default=0)
    rows = (_pdf_rows(doc, data, mapping, head_text, width, cfg.evidence, notes) if doc.kind == "pdf"
            else _tabular_rows(tables[0], data, _max_bytes(cfg.evidence)))
    for group, rnum, cells in rows:
        if (group, rnum) in answered:
            continue
        report["rows_read"] += 1
        if deadline and report["rows_read"] % 200 == 0 and time.time() > deadline:
            stopped = "agent_timeout:expand"
            break
        ordered = [cells[k] for k in sorted(cells)]
        texts = [str(c.get("raw") or c.get("value") or "") for c in ordered if c.get("type") == "string"]
        if texts and TOTAL_ROW.match(texts[0]):
            continue
        mapped = [c for col, c in cells.items() if col in col_to_field and str(c.get("raw") or c.get("value") or "").strip()]
        if len(mapped) < 2:
            continue
        if budget <= 0:
            stopped = f"records_truncated:{cfg.limits.get('max_records') or DEFAULT_MAX_RECORDS}"
            break
        rec, rec_reasons = _record(cfg, fields, doc, cells, mapping, constants, field_conf)
        first = next((c["ref"] for c in ordered if parse_loc(c["ref"])), None)
        out.append({"fields": rec, "reasons": rec_reasons, "_doc": doc.doc_id,
                    "_sort": parse_loc(first)[3] if first else ("", 0, 0, 0, rnum)})
        report["records"] += 1
        budget -= 1
    if notes:
        report["notes"] = notes[:50]
    incomplete = any(n.startswith(("pages_unread", "ocr_failed", "ocr_unavailable", "ocr_no_tables",
                                   "page_unreadable")) for n in notes)
    reasons = [stopped] if stopped else []
    report["complete"] = not stopped and not incomplete
    if stopped:
        report["stopped"] = stopped
    return {"records": out, "report": report, "reasons": reasons, "notes": notes}


def expand(cfg: Any, docs: list[Doc], items: list[Any], raw_records: list[dict[str, Any]], *,
           spool: Any = None, deadline: float | None = None, sandbox_mode: str = "off",
           memory_mb: int = 1024) -> dict[str, Any]:
    """Records for every table row the evidence did not hold. See the module docstring."""
    if cfg.dictionary.record_key != ROW_KEY:
        return {"records": [], "reports": [], "reasons": []}
    from . import sandbox
    max_records = int(cfg.limits.get("max_records") or DEFAULT_MAX_RECORDS)
    by_order = {getattr(i, "order", None): i for i in items}
    out: list[dict[str, Any]] = []
    reports, reasons = [], []
    budget = max_records - len(raw_records)
    for doc in docs:
        tables = _expandable(doc)
        if not tables:
            continue
        item = by_order.get(doc.order)
        if item is None:
            reports.append({"document": doc.source, "skipped": "source bytes unavailable", "complete": False})
            continue
        try:
            data = item.read(spool)
        except Exception as exc:                               # noqa: BLE001
            reports.append({"document": doc.source, "skipped": f"spool: {exc}"[:200], "complete": False})
            continue
        left = max(5.0, deadline - time.time()) if deadline else 600.0
        try:
            res = sandbox.call(expand_doc, cfg, doc, tables, data, raw_records, budget, deadline,
                               mode=sandbox_mode, timeout_s=left, memory_mb=memory_mb)
        except sandbox.SandboxError as exc:
            reports.append({"document": doc.source, "skipped": str(exc)[:300], "complete": False})
            reasons.append("agent_timeout:expand" if "timed out" in str(exc) else f"expand_failed:{doc.name}")
            continue
        out.extend(res["records"])
        reports.append(res["report"])
        reasons.extend(res["reasons"])
        budget -= len(res["records"])
    return {"records": out, "reports": reports, "reasons": reasons}


def _record(cfg, fields, doc, cells, mapping, constants, field_conf):
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
                if c.get("confidence") is not None:            # an OCR'd cell: the engine's confidence counts
                    conf *= 0.85 + 0.15 * float(c["confidence"])
                    if float(c["confidence"]) < float(cfg.evidence.get("ocr_min_confidence", 0.8)):
                        reasons.append(f"low_ocr_confidence:{name}")   # whether or not the field is required
                entry.update(value=value, confidence=round(conf, 4), source=f"{doc.doc_id}#{c['ref']}",
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
