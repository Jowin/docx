"""Reading headers and rows out of CSV and XLSX (the Phase 1 attachment types).

Deterministic and model-free: this is tool-layer work in the PRD's sense
(RT-27), so the same file always yields the same headers, rows and locators.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import filetypes


@dataclass
class Sheet:
    name: str
    headers: list[str] = field(default_factory=list)
    rows: list[list[Any]] = field(default_factory=list)
    #: Spreadsheet column letters aligned with ``headers`` (XLSX only).
    columns: list[str] = field(default_factory=list)
    #: 1-based row number of the header row in the source (XLSX only).
    header_row: int = 1

    def locator(self, filename: str, column_index: int, row_index: int) -> str:
        """CTR-13 source string pointing at one cell or field."""
        if self.columns:
            col = self.columns[column_index]
            return f"attachment:{filename}!{self.name}!{col}{self.header_row + 1 + row_index}"
        return f"attachment:{filename}#row{row_index + 2}col{column_index + 1}"


def read_csv(path: str | Path, max_rows: int = 500) -> list[Sheet]:
    p = Path(path)
    with p.open("r", encoding="utf-8-sig", newline="") as fh:
        sample = fh.read(8192)
        fh.seek(0)
        try:
            dialect: Any = csv.Sniffer().sniff(sample, delimiters=",;\t|")
        except csv.Error:
            dialect = csv.excel
        reader = csv.reader(fh, dialect)
        rows = [r for _, r in zip(range(max_rows + 1), reader)]
    if not rows:
        return [Sheet(name=p.stem)]
    return [Sheet(name=p.stem, headers=[h.strip() for h in rows[0]], rows=rows[1:])]


def read_xlsx(path: str | Path, max_rows: int = 500) -> list[Sheet]:
    from openpyxl import load_workbook
    from openpyxl.utils import get_column_letter

    wb = load_workbook(path, read_only=True, data_only=True)
    sheets: list[Sheet] = []
    try:
        for ws in wb.worksheets:
            grid = [list(r) for _, r in zip(range(max_rows + 1), ws.iter_rows(values_only=True))]
            header_row = _first_populated(grid)
            if header_row is None:
                sheets.append(Sheet(name=ws.title))
                continue
            raw_header = grid[header_row]
            keep = [i for i, v in enumerate(raw_header) if v is not None and str(v).strip()]
            sheets.append(
                Sheet(
                    name=ws.title,
                    headers=[str(raw_header[i]).strip() for i in keep],
                    rows=[[r[i] if i < len(r) else None for i in keep] for r in grid[header_row + 1 :]],
                    columns=[get_column_letter(i + 1) for i in keep],
                    header_row=header_row + 1,
                )
            )
    finally:
        wb.close()
    return sheets


def _first_populated(grid: list[list[Any]]) -> int | None:
    """Headers are rarely on row 1 in real workbooks; find the first row with
    two or more non-empty cells."""
    for idx, row in enumerate(grid):
        populated = [c for c in row if c is not None and str(c).strip()]
        if len(populated) >= 2:
            return idx
    return None


def read(path: str | Path) -> list[Sheet]:
    mime = filetypes.detect(path)
    if mime == filetypes.CSV:
        return read_csv(path)
    if mime in (filetypes.XLSX, filetypes.XLS):
        return read_xlsx(path)
    raise ValueError(f"unsupported_attachment:{mime}")
