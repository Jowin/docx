"""Excel Writer tool: an extraction result -> an .xlsx workbook.

Sheets:

* ``Records``: one row per record, scalar fields as typed cells (numbers stay
  numbers), plus ``Confidence``, ``Flagged`` and ``Flags`` for an extended
  result; flagged records are shaded.
* one sheet per array field (``line_items``): its rows, with the record they
  belong to in ``_record``;
* ``Sources`` (extended result): record, field, value, confidence and the
  locator each value came from;
* ``Run`` (extended result): status, flags, config, input and audit id.

Headers are frozen and filtered; text a spreadsheet would run as a formula is
prefixed with ``'``. Pure: fixed document timestamps, so the same result gives
the same workbook.
"""
from __future__ import annotations

import io
from datetime import datetime
from typing import Any

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter

from .result_table import cell, safe_text, shape

TOOL = "excel_writer"
MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
EXTENSION = "xlsx"
_EPOCH = datetime(2000, 1, 1)
_BOLD = Font(bold=True)
_FLAG = PatternFill("solid", fgColor="FFF4D6")
_MAX_SHEET_NAME = 31


def write_xlsx(result: Any) -> bytes:
    s = shape(result)
    wb = Workbook()
    ws = wb.active
    ws.title = "Records"
    meta_cols = ["Confidence", "Flagged", "Flags"] if s.extended else []
    _header(ws, ["_record", *s.columns, *meta_cols])
    for i, row in enumerate(s.rows, start=1):
        m = s.meta[i - 1]
        values = [i, *[safe_text(row[c]) for c in s.columns]]
        if meta_cols:
            values += [m.confidence, m.flagged, "; ".join(m.flags) or None]
        ws.append(values)
        if m.flagged:
            for c in ws[ws.max_row]:
                c.fill = _FLAG
    _finish(ws)

    for name, (cols, items) in s.arrays.items():
        sheet = wb.create_sheet(_sheet_name(name, wb))
        _header(sheet, ["_record", *cols])
        for it in items:
            sheet.append([it["_record"], *[safe_text(it.get(c)) for c in cols]])
        _finish(sheet)

    if s.extended:
        src = wb.create_sheet("Sources")
        _header(src, ["_record", "Field", "Value", "Confidence", "Source"])
        for i, (row, m) in enumerate(zip(s.rows, s.meta), start=1):
            for name, f in m.fields.items():
                if name in s.arrays:
                    continue
                src.append([i, name, safe_text(row.get(name)), f.get("confidence"), safe_text(f.get("source"))])
        _finish(src)
        run = wb.create_sheet("Run")
        md = s.metadata
        cfg, inp = md.get("config") or {}, md.get("input") or {}
        cls = md.get("classification") or {}
        for k, v in [("Flagged", s.flagged), ("Confidence", s.confidence), ("Flags", "; ".join(s.run_flags) or None),
                     ("Records", len(s.rows)),
                     ("Config", f"{cfg.get('client')}/{cfg.get('usecase')}@{cfg.get('version')}" if cfg else None),
                     ("Email type", cls.get("type")), ("Input", inp.get("name")), ("Input SHA-256", inp.get("sha256")),
                     ("Audit id", md.get("audit_id")), ("Processed at", md.get("processed_at")),
                     ("Engine", md.get("engine_version"))]:
            run.append([k, cell(v)])
            run.cell(run.max_row, 1).font = _BOLD
        run.column_dimensions["A"].width = 16
        run.column_dimensions["B"].width = 60

    for prop in ("created", "modified"):
        setattr(wb.properties, prop, _EPOCH)
    wb.properties.creator = "DataExtractor"
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _header(ws, names: list[str]) -> None:
    ws.append(names)
    for c in ws[1]:
        c.font = _BOLD


def _finish(ws) -> None:
    ws.freeze_panes = "A2"
    if ws.max_row > 1:
        ws.auto_filter.ref = ws.dimensions
    for col in range(1, ws.max_column + 1):
        letter = get_column_letter(col)
        width = max((len(str(c.value)) for c in ws[letter] if c.value is not None), default=8)
        ws.column_dimensions[letter].width = min(60, max(8, width + 2))


def _sheet_name(name: str, wb: Workbook) -> str:
    base = "".join(ch for ch in name if ch not in "[]:*?/\\")[:_MAX_SHEET_NAME] or "Items"
    out, n = base, 2
    while out in wb.sheetnames:
        out = f"{base[:_MAX_SHEET_NAME - 3]}_{n}"
        n += 1
    return out
