"""Spreadsheet Reader tool: XLSX/XLSM/XLS bytes -> sheets and typed cells.

Architecture Layer 4. Two operations:

  list_sheets(data)          inventory for the Sheet Selection Skill and the
                             Corpus Profiler: names, visibility, row counts,
                             hidden rows/columns and currency density.
  read_sheet(data, sheet)    one page of typed cells from one sheet, plus the
                             last rows of the sheet (where totals sit).

Format is decided from magic bytes, never the extension (RT-14). A zip that
is not a workbook, and an encrypted workbook, are typed errors.

v0.2 changes
  * XLSX is streamed (openpyxl read-only) so memory tracks the page, not the
    workbook. Merged ranges, hidden rows/columns and formulas with no cached
    value come from one streaming pass over the sheet XML.
  * Numbers are reported at Excel's own 15-significant-digit precision, so a
    stored 12400.499999999998 comes back as "12400.5", exactly as Excel shows.
  * Locale tags such as [$-409] no longer count as currency formats.
  * Cells in hidden rows or columns are marked ``hidden: true``.

v0.3 changes (sheets laid out like a document)
  * Currency amounts inside text ("Total Due: $12,400.00") are found, given
    character-span locators (E12:C12-21) and counted in currency density.
  * list_sheets reports text measures (text, long-text and "Label: value"
    cells) so the Sheet Selection Skill can tell a table from a document.
  * read_sheet(view="text") returns one line per row in reading order, so a
    document-style sheet can go to the Entity Extraction Skill like PDF text.
  * Content outside cells is reported and, where it is text, returned:
    images, charts, text boxes, comments, header/footer text, embedded files.
    XLS workbooks report ``outside_cells: {"inspected": false}``.

Values are the cached results Excel saved; this tool never evaluates formulas.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import io
import re
import warnings
import zipfile
import xml.etree.ElementTree as ET
from collections import deque
from decimal import Decimal
from posixpath import join as pjoin, normpath
from typing import Any, Iterator

from .common import (DEFAULT_MAX_BYTES, DEFAULT_MAX_UNCOMPRESSED, FILE_CORRUPT,
                     FILE_ENCRYPTED, FORMAT_MISMATCH, INPUT_TOO_LARGE, PARAM_INVALID,
                     SHEET_NOT_FOUND, ToolError, a1, check_size, col_index, envelope,
                     parse_a1_range, sniff_kind)
from .values import find_amounts

TOOL = "spreadsheet_reader"
OLE2 = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_ENCRYPTED_PACKAGE = "EncryptedPackage".encode("utf-16-le")
MAX_PAGE_ROWS = 5_000
MAX_LISTED = 1_000        # cap on hidden-row / merged-range lists in output

_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_RNS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_PKG = "{http://schemas.openxmlformats.org/package/2006/relationships}"
_CELL_REF = re.compile(r"^([A-Z]{1,3})(\d+)$")

# [$-409] / [$-F800] are locale or calendar tags, not currencies.
_LOCALE_TAG = re.compile(r"\[\$-[0-9A-Fa-f]+\]")
_CURRENCY_TAG = re.compile(r"\[\$([^\]\-]+)(?:-[0-9A-Fa-f]+)?\]")
_CURRENCY_SYM = re.compile(r"[$€£¥₹₩₽₺₪]")
_CURRENCY_ISO = re.compile(r"\b(USD|EUR|GBP|INR|JPY|CHF|CAD|AUD|SGD|AED)\b")


# ------------------------------------------------------------------ format

def detect_format(data: bytes, max_uncompressed: int = DEFAULT_MAX_UNCOMPRESSED) -> str:
    if data.startswith(b"PK\x03\x04"):
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                names = set(zf.namelist())
                total = sum(i.file_size for i in zf.infolist())
        except zipfile.BadZipFile as exc:
            raise ToolError(FILE_CORRUPT, f"bad zip container: {exc}") from exc
        if total > max_uncompressed:
            raise ToolError(INPUT_TOO_LARGE, f"uncompressed size {total} exceeds {max_uncompressed}",
                            detail={"uncompressed": total, "limit": max_uncompressed})
        if "xl/workbook.xml" in names:
            return "xlsx"
        if "xl/workbook.bin" in names:
            raise ToolError(FORMAT_MISMATCH, "xlsb workbooks are not supported",
                            detail={"detected": "xlsb"})
        raise ToolError(FORMAT_MISMATCH, "zip archive that is not an Excel workbook",
                        detail={"detected": "zip", "entries": sorted(names)[:20]})
    if data.startswith(OLE2):
        if _ENCRYPTED_PACKAGE in data:
            raise ToolError(FILE_ENCRYPTED, "password-protected OOXML workbook",
                            detail={"container": "ole2/EncryptedPackage"})
        return "xls"
    raise ToolError(FORMAT_MISMATCH, "not an XLSX or XLS workbook")


# ------------------------------------------------------------------ typing

def currency_of_format(fmt: str | None) -> str | None:
    """Currency symbol or code a number format displays, else None."""
    if not fmt or fmt == "General":
        return None
    f = _LOCALE_TAG.sub("", fmt)
    m = _CURRENCY_TAG.search(f)
    if m:
        return m.group(1).strip()
    f = _CURRENCY_TAG.sub("", f)
    m = _CURRENCY_SYM.search(f) or _CURRENCY_ISO.search(f)
    return m.group(0) if m else None


def _is_percent_fmt(fmt: str | None) -> bool:
    return bool(fmt) and "%" in re.sub(r'"[^"]*"', "", fmt)


def _excel_decimal(v: float) -> Decimal:
    """Excel shows and compares at 15 significant digits; so do we."""
    d = Decimal(format(v, ".15g"))
    return d if d != 0 else Decimal(0)


def _canon(d: Decimal) -> str:
    text = format(d.normalize(), "f") if d == d.to_integral_value() else format(d, "f")
    return "0" if text in ("-0", "") else text


def _number(v: float | int, fmt: str | None) -> dict[str, Any]:
    if isinstance(v, bool):
        return {"type": "boolean", "value": v}
    d = Decimal(v) if isinstance(v, int) else _excel_decimal(v)
    if _is_percent_fmt(fmt):
        return {"type": "percent", "value": _canon(d)}
    if d == d.to_integral_value() and abs(d) < 2 ** 53:
        return {"type": "integer", "value": int(d)}
    return {"type": "decimal", "value": _canon(d)}


def _when(v: dt.datetime | dt.date | dt.time) -> dict[str, Any]:
    if isinstance(v, dt.datetime):
        if v.time() == dt.time(0, 0):
            return {"type": "date", "value": v.date().isoformat()}
        return {"type": "datetime", "value": v.replace(microsecond=0).isoformat()}
    if isinstance(v, dt.date):
        return {"type": "date", "value": v.isoformat()}
    return {"type": "time", "value": v.replace(microsecond=0).isoformat()}


def _text(s: str) -> dict[str, Any]:
    s2 = s.strip()
    return {"type": "string", "value": s2} if s2 else {"type": "empty", "value": None}


# ------------------------------------------------------------------ sheet side-scan

class _Extras:
    """What the cell stream cannot tell us, per sheet."""

    def __init__(self) -> None:
        self.merged: list[str] = []
        self.hidden_rows: set[int] = set()
        self.hidden_cols: set[int] = set()
        self.uncached: dict[tuple[int, int], str] = {}
        self.rows_with_values = 0
        self.max_row = 0
        self.max_col = 0
        self.header_footer: dict[str, str] = {}

    def hidden(self, r: int, c: int) -> bool:
        return r in self.hidden_rows or c in self.hidden_cols


def _xlsx_sheet_paths(zf: zipfile.ZipFile) -> dict[str, str]:
    wb = ET.fromstring(zf.read("xl/workbook.xml"))
    rels = ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
    target = {r.get("Id"): r.get("Target") for r in rels.iter(f"{_PKG}Relationship")}
    out = {}
    for s in wb.iter(f"{_NS}sheet"):
        t = target.get(s.get(f"{_RNS}id"), "")
        out[s.get("name")] = t.lstrip("/") if t.startswith("/") else normpath(pjoin("xl", t))
    return out


def _scan_sheet_xml(zf: zipfile.ZipFile, path: str) -> _Extras:
    ex = _Extras()
    shared: dict[str, str] = {}
    try:
        with zf.open(path) as fh:
            for _, el in ET.iterparse(fh, events=("end",)):
                tag = el.tag
                if tag == f"{_NS}c":
                    m = _CELL_REF.match(el.get("r", ""))
                    has_v = el.find(f"{_NS}v") is not None or el.find(f"{_NS}is") is not None
                    if m and has_v:
                        ex.max_col = max(ex.max_col, col_index(m.group(1)))
                        ex.max_row = max(ex.max_row, int(m.group(2)))
                    f = el.find(f"{_NS}f")
                    if m and f is not None:
                        text = f.text or ""
                        if f.get("t") == "shared" and f.get("si") is not None:
                            text = shared.setdefault(f.get("si"), text) if text else \
                                shared.get(f.get("si"), f"shared:{f.get('si')}")
                        v = el.find(f"{_NS}v")
                        if v is None or v.text is None:
                            r, c = int(m.group(2)), col_index(m.group(1))
                            ex.uncached[(r, c)] = "=" + text if text else text
                            ex.max_col, ex.max_row = max(ex.max_col, c), max(ex.max_row, r)
                    el.clear()
                elif tag == f"{_NS}row":
                    if el.get("hidden") in ("1", "true"):
                        ex.hidden_rows.add(int(el.get("r", "0")))
                    el.clear()
                elif tag == f"{_NS}col" and el.get("hidden") in ("1", "true"):
                    ex.hidden_cols.update(range(int(el.get("min")), int(el.get("max")) + 1))
                elif tag == f"{_NS}mergeCell":
                    ex.merged.append(el.get("ref"))
                elif tag in _HF_TAGS and el.text:
                    text = _clean_header_footer(el.text)
                    if text:
                        ex.header_footer[tag.replace(_NS, "")] = text
    except (ET.ParseError, KeyError) as exc:
        raise ToolError(FILE_CORRUPT, f"unreadable sheet XML {path}: {exc}") from exc
    ex.merged.sort()
    return ex


_HF_TAGS = {f"{_NS}{t}" for t in ("oddHeader", "oddFooter", "evenHeader", "evenFooter",
                                   "firstHeader", "firstFooter")}
_HF_CODES = re.compile(r'&"[^"]*"|&\d+|&[LCRPNDTFAZGBIUESXYKH]|&&')
_XDR = "{http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing}"
_A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
_REL_KIND = {"drawing": "drawing", "comments": "comments", "oleObject": "embedded",
             "package": "embedded"}
_LABEL_VALUE = re.compile(r"^[^\W\d][^:\n]{0,40}:\s*\S")


def _clean_header_footer(raw: str) -> str:
    """'&L&"Arial,Bold"ACME&RPage &P of &N' -> 'ACME Page of'. Codes out, words in."""
    return " ".join(_HF_CODES.sub(" ", raw).split())


def _rels(zf: zipfile.ZipFile, part: str) -> list[tuple[str, str]]:
    """(kind suffix, resolved target path) for a part's relationships."""
    folder, name = part.rsplit("/", 1)
    rels_path = f"{folder}/_rels/{name}.rels"
    if rels_path not in zf.namelist():
        return []
    out = []
    for r in ET.fromstring(zf.read(rels_path)).iter(f"{_PKG}Relationship"):
        if r.get("TargetMode") == "External":
            continue
        kind = r.get("Type", "").rsplit("/", 1)[-1]
        target = r.get("Target", "")
        path = target.lstrip("/") if target.startswith("/") else normpath(pjoin(folder, target))
        out.append((kind, path))
    return out


def _anchor_ref(anchor) -> str | None:
    frm = anchor.find(f"{_XDR}from")
    if frm is None:
        return None
    col, row = frm.find(f"{_XDR}col"), frm.find(f"{_XDR}row")
    try:
        return a1(int(row.text) + 1, int(col.text) + 1)
    except (AttributeError, TypeError, ValueError):
        return None


def _shape_text(sp) -> str:
    paras = []
    for p in sp.iter(f"{_A}p"):
        paras.append("".join(t.text or "" for t in p.iter(f"{_A}t")))
    return "\n".join(x for x in paras if x.strip()).strip()


def _scan_outside(zf: zipfile.ZipFile, sheet_path: str) -> dict[str, Any]:
    """Everything on a sheet that is not a cell value."""
    out: dict[str, Any] = {"images": [], "charts": [], "text_boxes": [], "comments": [],
                           "embedded": []}
    for kind, path in _rels(zf, sheet_path):
        role = _REL_KIND.get(kind)
        try:
            if role == "drawing":
                root = ET.fromstring(zf.read(path))
                for anchor in root:
                    ref = _anchor_ref(anchor)
                    for pic in anchor.iter(f"{_XDR}pic"):
                        out["images"].append({"anchor": ref})
                    for gf in anchor.iter(f"{_XDR}graphicFrame"):
                        uri = next((g.get("uri") for g in gf.iter(f"{_A}graphicData")), "")
                        if uri.endswith("/chart"):
                            out["charts"].append({"anchor": ref})
                    for sp in anchor.iter(f"{_XDR}sp"):
                        text = _shape_text(sp)
                        if text:
                            out["text_boxes"].append({"anchor": ref, "text": text})
            elif role == "comments":
                root = ET.fromstring(zf.read(path))
                authors = [a.text or "" for a in root.iter(f"{_NS}author")]
                for c in root.iter(f"{_NS}comment"):
                    aid = int(c.get("authorId", "-1"))
                    text = "".join(t.text or "" for t in c.iter(f"{_NS}t")).strip()
                    out["comments"].append({"ref": c.get("ref"), "text": text,
                                            "author": authors[aid] if 0 <= aid < len(authors) else None})
            elif role == "embedded":
                out["embedded"].append({"path": path})
        except (ET.ParseError, KeyError) as exc:
            raise ToolError(FILE_CORRUPT, f"unreadable part {path}: {exc}") from exc
    for k in out:
        out[k].sort(key=_sort_key)
    return out


def _sort_key(d: dict) -> tuple:
    return tuple(str(d.get(k) or "") for k in sorted(d))


def _workbook_embedded(zf: zipfile.ZipFile) -> list[dict[str, Any]]:
    out = []
    for name in sorted(n for n in zf.namelist() if n.startswith("xl/embeddings/")):
        blob = zf.read(name)
        out.append({"path": name, "bytes": len(blob),
                    "sha256": hashlib.sha256(blob).hexdigest(),
                    "detected": sniff_kind(blob)})
    return out


# ------------------------------------------------------------------ backends
# Both backends expose:
#   sheets()                          [(name, kind, state)]
#   extras(name)                      _Extras
#   rows(name, r1, r2, c1, c2)        iterator of (row, [(col, typed, fmt)])
#                                     for non-empty rows; r2 None = to the end

class _Xlsx:
    def __init__(self, data: bytes) -> None:
        import openpyxl
        self._buf = io.BytesIO(data)
        self._zf = zipfile.ZipFile(io.BytesIO(data))
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            try:
                self.wb = openpyxl.load_workbook(self._buf, read_only=True, data_only=True)
                self._paths = _xlsx_sheet_paths(self._zf)
            except ToolError:
                raise
            except Exception as exc:  # openpyxl raises a zoo of types
                raise ToolError(FILE_CORRUPT, f"cannot open workbook: {exc}") from exc
        self._extras: dict[str, _Extras] = {}
        self._outside: dict[str, dict | None] = {}

    def close(self) -> None:
        self.wb.close()
        self._zf.close()

    def sheets(self):
        from openpyxl.chartsheet import Chartsheet
        return [(n, "chart" if isinstance(self.wb[n], Chartsheet) else "worksheet",
                 self.wb[n].sheet_state) for n in self.wb.sheetnames]

    def outside(self, name) -> dict[str, Any] | None:
        if name not in self._outside:
            path = self._paths.get(name)
            self._outside[name] = _scan_outside(self._zf, path) if path else None
        return self._outside[name]

    def embedded(self) -> list[dict[str, Any]] | None:
        return _workbook_embedded(self._zf)

    def extras(self, name) -> _Extras:
        if name not in self._extras:
            path = self._paths.get(name)
            self._extras[name] = _scan_sheet_xml(self._zf, path) if path else _Extras()
        return self._extras[name]

    def rows(self, name, r1, r2, c1, c2) -> Iterator[tuple[int, list]]:
        ws = self.wb[name]
        ex = self.extras(name)
        ws.reset_dimensions()          # never trust <dimension>; read to the real end
        r2 = r2 if r2 is not None else max(ex.max_row, r1)
        by_row: dict[int, list] = {}
        for (r, c), f in ex.uncached.items():
            if r1 <= r <= r2 and c1 <= c <= c2:
                by_row.setdefault(r, []).append(
                    (c, {"type": "formula_uncached", "value": None, "formula": f}, None))
        for row in ws.iter_rows(min_row=r1, max_row=r2, min_col=c1, max_col=c2):
            rnum, out = None, []
            for i, cell in enumerate(row):
                c = c1 + i
                if getattr(cell, "row", None):
                    rnum = cell.row
                v = cell.value
                if v is None:
                    continue
                fmt = getattr(cell, "number_format", None)
                if cell.data_type == "e":
                    typed = {"type": "error", "value": str(v)}
                elif isinstance(v, (dt.datetime, dt.date, dt.time)):
                    typed = _when(v)
                elif isinstance(v, (int, float)):
                    typed = _number(v, fmt)
                else:
                    typed = _text(str(v))
                    if typed["type"] == "empty":
                        continue
                out.append((c, typed, fmt))
            if rnum is None:
                continue
            out.extend(by_row.get(rnum, ()))
            if out:
                yield rnum, sorted(out, key=lambda t: t[0])


class _Xls:
    def __init__(self, data: bytes) -> None:
        import xlrd
        try:
            self.wb = xlrd.open_workbook(file_contents=data, formatting_info=True,
                                         on_demand=True)
        except xlrd.XLRDError as exc:
            if "encrypt" in str(exc).lower():
                raise ToolError(FILE_ENCRYPTED, "password-protected XLS workbook") from exc
            raise ToolError(FILE_CORRUPT, f"cannot open workbook: {exc}") from exc
        except Exception as exc:
            raise ToolError(FILE_CORRUPT, f"cannot open workbook: {exc}") from exc
        self._extras: dict[str, _Extras] = {}

    def close(self) -> None:
        self.wb.release_resources()

    def outside(self, name) -> None:
        return None                    # xlrd does not expose drawings or comments text

    def embedded(self) -> None:
        return None

    def sheets(self):
        states = {0: "visible", 1: "hidden", 2: "veryHidden"}
        out = []
        for i, name in enumerate(self.wb.sheet_names()):
            vis = self.wb.sheet_by_index(i).visibility
            out.append((name, "worksheet", states.get(vis, "visible")))
        return out

    def extras(self, name) -> _Extras:
        if name not in self._extras:
            s = self.wb.sheet_by_name(name)
            ex = _Extras()
            ex.max_row, ex.max_col = s.nrows, s.ncols
            ex.merged = sorted(f"{a1(rlo + 1, clo + 1)}:{a1(rhi, chi)}"
                               for rlo, rhi, clo, chi in s.merged_cells)
            ex.hidden_rows = {r + 1 for r, info in s.rowinfo_map.items() if info.hidden}
            ex.hidden_cols = {c + 1 for c, info in s.colinfo_map.items() if info.hidden}
            self._extras[name] = ex
        return self._extras[name]

    def _fmt(self, sheet, r, c):
        try:
            xf = self.wb.xf_list[sheet.cell_xf_index(r, c)]
            return self.wb.format_map[xf.format_key].format_str
        except (IndexError, KeyError):
            return None

    def rows(self, name, r1, r2, c1, c2):
        import xlrd
        s = self.wb.sheet_by_name(name)
        r2 = s.nrows if r2 is None else min(r2, s.nrows)
        for r in range(r1 - 1, r2):
            out = []
            for c in range(c1 - 1, min(c2, s.ncols)):
                ctype, v = s.cell_type(r, c), s.cell_value(r, c)
                if ctype in (xlrd.XL_CELL_EMPTY, xlrd.XL_CELL_BLANK):
                    continue
                fmt = self._fmt(s, r, c)
                if ctype == xlrd.XL_CELL_DATE:
                    try:
                        typed = _when(xlrd.xldate.xldate_as_datetime(v, self.wb.datemode))
                    except Exception:
                        typed = _number(v, fmt)
                elif ctype == xlrd.XL_CELL_NUMBER:
                    typed = _number(v, fmt)
                elif ctype == xlrd.XL_CELL_BOOLEAN:
                    typed = {"type": "boolean", "value": bool(v)}
                elif ctype == xlrd.XL_CELL_ERROR:
                    typed = {"type": "error", "value": xlrd.error_text_from_code.get(v, str(v))}
                else:
                    typed = _text(str(v))
                    if typed["type"] == "empty":
                        continue
                out.append((c + 1, typed, fmt))
            if out:
                yield r + 1, out


def _open(data: bytes, max_bytes: int):
    check_size(data, max_bytes)
    kind = detect_format(data)
    return kind, (_Xlsx(data) if kind == "xlsx" else _Xls(data))


def _resolve_sheet(book, sheet: str) -> tuple[str, str, str]:
    for name, kind, state in book.sheets():
        if name == sheet:
            return name, kind, state
    names = [n for n, _, _ in book.sheets()]
    raise ToolError(SHEET_NOT_FOUND, f"no sheet named {sheet!r}", detail={"sheets": names})


def _cell_out(r: int, c: int, typed: dict, fmt: str | None, ex: _Extras,
              merged_tl: dict[str, str]) -> dict[str, Any]:
    ref = a1(r, c)
    out = {"ref": ref, **typed}
    if fmt and fmt != "General":
        out["number_format"] = fmt
    if typed["type"] in ("integer", "decimal"):
        cur = currency_of_format(fmt)
        if cur:
            out["currency_format"] = True
            out["currency_symbol"] = cur
    if typed["type"] == "string":
        amounts = find_amounts(typed["value"])
        if amounts:
            out["amounts"] = [{**a, "locator": f"{ref}:C{a['start'] + 1}-{a['end']}"}
                              for a in amounts]
    if ref in merged_tl:
        out["merged_range"] = merged_tl[ref]
    if ex.hidden(r, c):
        out["hidden"] = True
    return out


def _capped(values: list) -> dict[str, Any]:
    return {"count": len(values), "items": values[:MAX_LISTED],
            "truncated": len(values) > MAX_LISTED}


# ------------------------------------------------------------------ operations

def list_sheets(data: bytes, filename: str | None = None, *, preview_rows: int = 5,
                sample_rows: int = 1_000, max_bytes: int = DEFAULT_MAX_BYTES) -> dict[str, Any]:
    """Inventory every sheet with the statistics the Sheet Selection Skill uses.

    Density figures come from the first ``sample_rows`` non-empty rows of each
    sheet; ``row_count`` and hidden/merged counts cover the whole sheet.
    """
    params = {"preview_rows": preview_rows, "sample_rows": sample_rows}
    if not 0 <= preview_rows <= 50 or not 1 <= sample_rows <= 50_000:
        raise ToolError(PARAM_INVALID, "preview_rows 0..50, sample_rows 1..50000")
    kind, book = _open(data, max_bytes)
    try:
        sheets = []
        for idx, (name, skind, state) in enumerate(book.sheets()):
            entry: dict[str, Any] = {"index": idx, "name": name, "kind": skind, "state": state}
            if skind != "worksheet":
                sheets.append(entry)
                continue
            ex = book.extras(name)
            non_empty = numeric = cur_fmt = cur_text = dates = sampled = 0
            text_cells = long_text = label_value = 0
            preview = []
            if ex.max_row and ex.max_col:
                for r, cells in book.rows(name, 1, None, 1, ex.max_col):
                    sampled += 1
                    for c, typed, fmt in cells:
                        non_empty += 1
                        t = typed["type"]
                        if t in ("integer", "decimal", "percent"):
                            numeric += 1
                            cur_fmt += bool(currency_of_format(fmt))
                        elif t in ("date", "datetime"):
                            dates += 1
                        elif t == "string":
                            v = typed["value"]
                            text_cells += 1
                            long_text += len(v) >= 40
                            label_value += bool(_LABEL_VALUE.match(v))
                            cur_text += bool(find_amounts(v))
                    if len(preview) < preview_rows:
                        preview.append({"row": r, "cells": [f"{a1(r, c)}={t['value']}"
                                                             for c, t, _ in cells]})
                    if sampled >= sample_rows:
                        break
            entry.update({
                "dimension": f"A1:{a1(ex.max_row, ex.max_col)}" if ex.max_row else None,
                "row_count": ex.max_row,
                "sampled_rows": sampled,
                "sample_complete": sampled < sample_rows,
                "non_empty_cells": non_empty,
                "numeric_cells": numeric,
                "date_cells": dates,
                "currency_formatted_cells": cur_fmt,
                "currency_text_cells": cur_text,
                "currency_density": round((cur_fmt + cur_text) / non_empty, 4) if non_empty else 0.0,
                "text_cells": text_cells,
                "long_text_cells": long_text,
                "label_value_cells": label_value,
                "merged_ranges": len(ex.merged),
                "hidden_rows": len(ex.hidden_rows),
                "hidden_columns": len(ex.hidden_cols),
                "uncached_formulas": len(ex.uncached),
                "outside_cells": _outside_counts(book.outside(name), ex),
                "preview": preview,
            })
            sheets.append(entry)
        embedded = book.embedded()
    finally:
        book.close()
    result = {"format": kind, "sheet_count": len(sheets), "sheets": sheets,
              "embedded_objects": embedded}
    return envelope(TOOL + ".list_sheets", data, filename, params, result)


def read_sheet(data: bytes, filename: str | None = None, *, sheet: str,
               cell_range: str | None = None, start_row: int = 1, row_limit: int = 50,
               tail_rows: int = 5, view: str = "cells", max_cols: int = 2_000,
               max_bytes: int = DEFAULT_MAX_BYTES) -> dict[str, Any]:
    """One page of typed, sparse cells from one sheet. Absent refs are empty.

    view         'cells' (typed cells), 'text' (one line per row in reading
                 order, for document-style sheets read like PDF text), or
                 'both'. The text view leaves out hidden rows and columns.

    cell_range   'B2:F40' reads exactly that block (no paging, no tail).
    start_row    first sheet row of the page (1-based)
    row_limit    sheet rows per page, 1..5000; default 50 is a sample for
                 pattern recognition. Rows count from start_row, so title
                 blocks and blank rows above a table use part of the page.
    tail_rows    last N non-empty rows of the sheet, 0..50, when outside the page
    """
    params = {"sheet": sheet, "range": cell_range, "start_row": start_row,
              "row_limit": row_limit, "tail_rows": tail_rows, "view": view,
              "max_cols": max_cols}
    if view not in ("cells", "text", "both"):
        raise ToolError(PARAM_INVALID, "view must be cells, text or both")
    if start_row < 1 or not 1 <= row_limit <= MAX_PAGE_ROWS or not 0 <= tail_rows <= 50:
        raise ToolError(PARAM_INVALID, f"start_row >= 1, row_limit 1..{MAX_PAGE_ROWS}, tail_rows 0..50")
    kind, book = _open(data, max_bytes)
    try:
        name, skind, state = _resolve_sheet(book, sheet)
        if skind != "worksheet":
            raise ToolError(SHEET_NOT_FOUND, f"{sheet!r} is a {skind} sheet with no cells")
        ex = book.extras(name)
        if cell_range:
            r1, c1, r2, c2 = parse_a1_range(cell_range)
            if r2 - r1 + 1 > MAX_PAGE_ROWS:
                raise ToolError(INPUT_TOO_LARGE, f"range spans {r2 - r1 + 1} rows, limit {MAX_PAGE_ROWS}",
                                detail={"rows": r2 - r1 + 1, "limit": MAX_PAGE_ROWS})
            tail_rows = 0
        else:
            r1, r2 = start_row, start_row + row_limit - 1
            c1, c2 = 1, max(ex.max_col, 1)
        if c2 - c1 + 1 > max_cols:
            raise ToolError(INPUT_TOO_LARGE, f"{c2 - c1 + 1} columns, limit {max_cols}; pass cell_range",
                            detail={"columns": c2 - c1 + 1, "limit": max_cols})

        merged_tl = {m.split(":")[0]: m for m in ex.merged}
        page, tail = [], deque(maxlen=tail_rows)
        last_row = 0
        scan_to = r2 if not tail_rows else None      # tail needs the whole sheet
        if ex.max_row:
            for r, cells in book.rows(name, r1, scan_to, c1, c2):
                last_row = max(last_row, r)
                if r1 <= r <= r2:
                    page.append({"row": r, "cells": [_cell_out(r, c, t, f, ex, merged_tl)
                                                     for c, t, f in cells]})
                elif r > r2 and tail_rows:
                    tail.append((r, cells))
        tail_out = [{"row": r, "cells": [_cell_out(r, c, t, f, ex, merged_tl) for c, t, f in cells]}
                    for r, cells in tail]

        in_window = lambda rng: _overlaps(rng, r1, c1, r2, c2)  # noqa: E731
        merged = [m for m in ex.merged if in_window(m)]
        hidden_rows = sorted(r for r in ex.hidden_rows if r1 <= r <= r2)
        has_more = not cell_range and ex.max_row > r2
        outside = book.outside(name)
    finally:
        book.close()

    result = {
        "format": kind,
        "sheet": name,
        "state": state,
        "dimension": f"A1:{a1(ex.max_row, ex.max_col)}" if ex.max_row else None,
        "row_count": ex.max_row,
        "range": f"{a1(r1, c1)}:{a1(r2, c2)}",
        "ref_prefix": f"{name}!",
        "page": None if cell_range else {
            "start_row": r1, "row_limit": row_limit, "returned_rows": len(page),
            "has_more": has_more, "next_start_row": r2 + 1 if has_more else None},
        "merged_ranges": merged,
        "hidden_rows": _capped(hidden_rows),
        "hidden_columns": sorted(ex.hidden_cols),
        "cell_count": sum(len(p["cells"]) for p in page),
        "outside_cells": _outside_full(outside, ex),
    }
    if view in ("cells", "both"):
        result.update(rows=page, tail=tail_out)
    if view in ("text", "both"):
        result.update(text_lines=_text_lines(page, ex), tail_text_lines=_text_lines(tail_out, ex))
    return envelope(TOOL + ".read_sheet", data, filename, params, result)


def _display(cell: dict[str, Any]) -> str:
    t, v = cell["type"], cell.get("value")
    if t == "formula_uncached":
        return f"[formula {cell.get('formula')}, not calculated]"
    if t == "percent":
        return f"{(Decimal(v) * 100).normalize():f}%"
    if t in ("integer", "decimal") and cell.get("currency_symbol"):
        return f"{cell['currency_symbol']}{v}"
    if t == "boolean":
        return "TRUE" if v else "FALSE"
    return str(v)


def _text_lines(rows: list[dict[str, Any]], ex: _Extras) -> list[dict[str, Any]]:
    """One line per row, 'B4: Invoice No: INV-20194 | E4: ...', visible cells only."""
    out = []
    for row in rows:
        cells = [c for c in row["cells"] if not c.get("hidden")]
        if not cells:
            continue
        line: dict[str, Any] = {"row": row["row"], "locator": cells[0]["ref"],
                                "text": " | ".join(f"{c['ref']}: {_display(c)}" for c in cells)}
        amounts = []
        for c in cells:
            amounts.extend(c.get("amounts", []))
            if c.get("currency_format"):
                amounts.append({"text": _display(c), "value": str(c["value"]),
                                "currency": c["currency_symbol"], "locator": c["ref"]})
        if amounts:
            line["amounts"] = amounts
        out.append(line)
    return out


def _outside_counts(outside: dict | None, ex: _Extras) -> dict[str, Any]:
    if outside is None:
        return {"inspected": False}
    counts = {k: len(v) for k, v in outside.items()}
    return {"inspected": True, **counts, "header_footer": bool(ex.header_footer)}


def _outside_full(outside: dict | None, ex: _Extras) -> dict[str, Any]:
    if outside is None:
        return {"inspected": False}
    return {"inspected": True, **outside, "header_footer": dict(sorted(ex.header_footer.items()))}


def _overlaps(rng: str, r1: int, c1: int, r2: int, c2: int) -> bool:
    mr1, mc1, mr2, mc2 = parse_a1_range(rng)
    return mr1 <= r2 and mr2 >= r1 and mc1 <= c2 and mc2 >= c1
