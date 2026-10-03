"""PDF Text Tool: text-layer PDF bytes -> page inventory + one window of pages.

Architecture Layer 4. Used by the PDF Parser Agent (Phase 2).

For the whole document (cheap, no layout analysis):
  page inventory, encryption state, producer, form fields, embedded files.
For a window of pages (``start_page`` / ``page_limit``) plus the last page:
  lines with bounding boxes and locators, tables, currency amounts found in
  lines, annotations, and the measurements the PDF Parser Agent uses to decide
  on OCR (RT-16): character count, text density, image coverage and the share
  of characters that decoded to garbage. The decision is the agent's; this
  tool never calls OCR.

Hidden text: characters drawn in white, at under 1pt, or outside the page are
marked on their line (``hidden``) and left out of the page ``text`` — the
page text is what a person would see. Lines are still returned so the
review queue can show them; a PDF that says one thing and hides another is
a signal, not noise.

Locators (combine with the attachment name to form a CTR-13 source):
  p3            page 3
  p3:L12        line 12 on page 3 (1-based, top to bottom)
  p3:L12:C5-15  characters 5..15 of that line (an amount inside it)
  p3:T1:R2C4    table 1 on page 3, row 2, column 4
  p3:A1         annotation 1 on page 3
  form:<name>   form field

Encrypted PDFs: an owner-password-only PDF (opens with an empty user
password) is read normally and reported as such. A PDF needing a user
password raises ``file_encrypted``; the Encryption Agent and Decryption
Tool own that path.
"""
from __future__ import annotations

import hashlib
import io
import logging
import re
from typing import Any

from .common import (DEFAULT_MAX_BYTES, FILE_CORRUPT, FILE_ENCRYPTED, FORMAT_MISMATCH,
                     INPUT_TOO_LARGE, PARAM_INVALID, ToolError, check_size, envelope,
                     sniff_kind)
from .values import find_amounts

TOOL = "pdf_text"
MAX_PAGE_WINDOW = 50
MAX_LISTED = 500
_TEXT_OPS = re.compile(rb"T[jJ]\b|['\"]\s*$", re.M)
_TABLE_SETTINGS = {
    "lines": {},
    "text": {"vertical_strategy": "text", "horizontal_strategy": "text"},
}

for _noisy in ("pdfminer", "pypdf"):
    logging.getLogger(_noisy).setLevel(logging.ERROR)


def _r(x: float) -> float:
    return round(float(x), 2)


def _s(v: Any) -> Any:
    """pypdf objects -> JSON-native values."""
    if v is None:
        return None
    if isinstance(v, (bool, int, float)):
        return v
    if isinstance(v, (list, tuple)):
        return [_s(x) for x in v]
    return str(v)


# ------------------------------------------------------------------ document level

def _open_reader(data: bytes):
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError
    try:
        reader = PdfReader(io.BytesIO(data), strict=False)
    except (PdfReadError, ValueError, OSError) as exc:
        raise ToolError(FILE_CORRUPT, f"cannot parse PDF: {exc}") from exc
    state = {"pdf_version": reader.pdf_header.replace("%PDF-", ""), "encrypted": False,
             "owner_password_only": False}
    if reader.is_encrypted:
        state["encrypted"] = True
        try:
            ok = reader.decrypt("")
        except Exception:
            ok = 0
        if not ok:
            raise ToolError(FILE_ENCRYPTED, "PDF requires a user password",
                            detail={"pdf_version": state["pdf_version"]})
        state["owner_password_only"] = True
    return reader, state


def _inventory(reader) -> list[dict[str, Any]]:
    """Per-page facts from the object tree only — no layout, so cheap for 500 pages."""
    out = []
    for pno, page in enumerate(reader.pages, start=1):
        entry: dict[str, Any] = {"page": pno}
        try:
            res = page.get("/Resources")
            res = res.get_object() if res is not None else {}
            xobj = res.get("/XObject")
            xobj = xobj.get_object() if xobj is not None else {}
            entry["image_objects"] = sum(
                1 for k in xobj if xobj[k].get_object().get("/Subtype") == "/Image")
            contents = page.get_contents()
            entry["has_text_operators"] = bool(contents is not None and
                                               _TEXT_OPS.search(contents.get_data()))
        except Exception as exc:
            entry.update(error=FILE_CORRUPT, message=str(exc)[:200])
        out.append(entry)
    return out


def _form_fields(reader) -> list[dict[str, Any]]:
    try:
        fields = reader.get_fields() or {}
    except Exception:
        return []
    out = []
    for name in sorted(fields):
        f = fields[name]
        ftype = f.get("/FT")
        if ftype == "/Sig":
            value = "signed" if f.get("/V") is not None else None
        else:
            value = _s(f.get("/V"))
        out.append({"locator": f"form:{name}", "name": name,
                    "type": {"/Tx": "text", "/Btn": "button", "/Ch": "choice",
                             "/Sig": "signature"}.get(str(ftype), _s(ftype)),
                    "value": value})
    return out[:MAX_LISTED]


def _embedded_files(reader) -> list[dict[str, Any]]:
    try:
        attachments = reader.attachments
    except Exception:
        return []
    out = []
    for name in sorted(attachments):
        for i, blob in enumerate(attachments[name]):
            out.append({"name": name, "index": i, "bytes": len(blob),
                        "sha256": hashlib.sha256(blob).hexdigest(),
                        "detected": sniff_kind(blob)})
    return out[:MAX_LISTED]


def _annotations(page, pno: int, height: float) -> list[dict[str, Any]]:
    try:
        annots = page.get("/Annots")
        annots = annots.get_object() if annots is not None else []
    except Exception:
        return []
    out = []
    for a in annots:
        a = a.get_object()
        subtype = str(a.get("/Subtype", ""))
        if subtype in ("/Link", "/Widget", "/Popup"):
            continue                       # links, form widgets, note popups
        text = a.get("/Contents")
        if not text:
            continue
        rect = [float(x) for x in a.get("/Rect", [0, 0, 0, 0])]
        out.append({"locator": f"p{pno}:A{len(out) + 1}", "type": subtype.lstrip("/"),
                    "text": str(text),
                    "bbox": [_r(rect[0]), _r(height - rect[3]), _r(rect[2]), _r(height - rect[1])]})
    return out


# ------------------------------------------------------------------ page level

def _is_white(ch: dict) -> bool:
    col = ch.get("non_stroking_color")
    if col is None or isinstance(col, str):
        return False
    col = tuple(col) if isinstance(col, (list, tuple)) else (col,)
    if not all(isinstance(c, (int, float)) for c in col):
        return False                        # pattern fills
    if len(col) == 1:
        return col[0] >= 0.98
    if len(col) == 3:
        return min(col) >= 0.98
    if len(col) == 4:
        return max(col) <= 0.02
    return False


def _hidden_reasons(ch: dict, width: float, height: float) -> set[str]:
    reasons = set()
    if _is_white(ch):
        reasons.add("white_text")
    if float(ch.get("size", 10)) < 1.0:
        reasons.add("tiny_text")
    if ch["x1"] < 0 or ch["x0"] > width or ch["bottom"] < 0 or ch["top"] > height:
        reasons.add("off_page")
    return reasons


def _garbled(text: str) -> bool:
    if text.startswith("(cid:") or text == "�":
        return True
    return any(0xE000 <= ord(c) <= 0xF8FF or (ord(c) < 32 and c not in "\t\n\r") for c in text)


def _page(page, pypdf_page, pno: int, include_tables: bool, table_strategy: str,
          include_amounts: bool) -> dict[str, Any]:
    width, height = float(page.width), float(page.height)
    area = max(width * height, 1.0)
    visible = [c for c in page.chars if c.get("text", "").strip()]
    garbled = sum(1 for c in visible if _garbled(c["text"]))
    hidden_chars = sum(1 for c in visible if _hidden_reasons(c, width, height))

    img_area = 0.0
    for im in page.images:
        x0, x1 = max(im["x0"], 0), min(im["x1"], width)
        top, bottom = max(im["top"], 0), min(im["bottom"], height)
        if x1 > x0 and bottom > top:
            img_area += (x1 - x0) * (bottom - top)

    lines, shown = [], []
    for i, ln in enumerate(page.extract_text_lines(return_chars=True, strip=True), start=1):
        loc = f"p{pno}:L{i}"
        entry: dict[str, Any] = {"line": i, "locator": loc, "text": ln["text"],
                                 "bbox": [_r(ln["x0"]), _r(ln["top"]), _r(ln["x1"]), _r(ln["bottom"])]}
        chars = [c for c in ln.get("chars", []) if c.get("text", "").strip()]
        flagged = [(_hidden_reasons(c, width, height)) for c in chars]
        n_hidden = sum(1 for f in flagged if f)
        if chars and n_hidden * 2 >= len(chars):          # most of the line is hidden
            entry["hidden"] = sorted(set().union(*flagged))
        else:
            shown.append(ln["text"])
        if include_amounts:
            amounts = find_amounts(ln["text"])
            if amounts:
                entry["amounts"] = [{**a, "locator": f"{loc}:C{a['start'] + 1}-{a['end']}"}
                                    for a in amounts]
        lines.append(entry)

    out: dict[str, Any] = {
        "page": pno,
        "locator": f"p{pno}",
        "width": _r(width),
        "height": _r(height),
        "rotation": int(page.rotation or 0),
        "char_count": len(visible),
        "has_text_layer": len(visible) > 0,
        # visible characters per 1,000 square points; a dense A4 text page is ~5-10
        "text_density": round(len(visible) / area * 1000, 3),
        # share of characters with no usable Unicode mapping; high = text layer is junk
        "garbled_ratio": round(garbled / len(visible), 4) if visible else 0.0,
        "hidden_char_count": hidden_chars,
        "image_count": len(page.images),
        # capped at 1.0; overlapping images can sum past the page area
        "image_coverage": round(min(img_area / area, 1.0), 4),
        "text": "\n".join(shown),
        "lines": lines,
        "annotations": _annotations(pypdf_page, pno, height),
    }
    if include_tables:
        tables = []
        for t, found in enumerate(page.find_tables(_TABLE_SETTINGS[table_strategy]), start=1):
            rows = found.extract()
            x0, top, x1, bottom = found.bbox
            tables.append({
                "table": t,
                "locator": f"p{pno}:T{t}",
                "strategy": table_strategy,
                "bbox": [_r(x0), _r(top), _r(x1), _r(bottom)],
                "rows": [[{"locator": f"p{pno}:T{t}:R{r}C{c}", "text": cell}
                          for c, cell in enumerate(row, start=1) if cell not in (None, "")]
                         for r, row in enumerate(rows, start=1)],
            })
        out["tables"] = tables
    return out


# ------------------------------------------------------------------ operation

def extract_pdf_text(data: bytes, filename: str | None = None, *, start_page: int = 1,
                     page_limit: int = 10, tail_pages: int = 1, include_tables: bool = True,
                     table_strategy: str = "lines", include_amounts: bool = True,
                     max_pages: int = 1_000, max_bytes: int = DEFAULT_MAX_BYTES) -> dict[str, Any]:
    """Read a PDF attachment.

    start_page      first page of the window (1-based)
    page_limit      pages in the window, 1..50; default 10 covers most invoices
                    and statements whole
    tail_pages      last N pages when outside the window, 0..5 (totals, signatures)
    table_strategy  'lines' (ruled tables) | 'text' (borderless: aligned columns).
                    'text' finds more tables and more false ones; use it on
                    pages known to hold a table.
    include_amounts currency amounts found in each line, with char-span locators
    """
    params = {"start_page": start_page, "page_limit": page_limit, "tail_pages": tail_pages,
              "include_tables": include_tables, "table_strategy": table_strategy,
              "include_amounts": include_amounts, "max_pages": max_pages}
    if start_page < 1 or not 1 <= page_limit <= MAX_PAGE_WINDOW or not 0 <= tail_pages <= 5:
        raise ToolError(PARAM_INVALID,
                        f"start_page >= 1, page_limit 1..{MAX_PAGE_WINDOW}, tail_pages 0..5")
    if table_strategy not in _TABLE_SETTINGS:
        raise ToolError(PARAM_INVALID, "table_strategy must be 'lines' or 'text'")
    check_size(data, max_bytes)
    if b"%PDF-" not in data[:1024]:
        raise ToolError(FORMAT_MISMATCH, "no %PDF- header in the first 1024 bytes")

    reader, state = _open_reader(data)
    try:
        n_pages = len(reader.pages)
    except Exception as exc:
        raise ToolError(FILE_CORRUPT, f"cannot read page tree: {exc}") from exc
    if n_pages > max_pages:
        raise ToolError(INPUT_TOO_LARGE, f"{n_pages} pages, limit {max_pages}",
                        detail={"pages": n_pages, "limit": max_pages})
    if start_page > max(n_pages, 1):
        raise ToolError(PARAM_INVALID, f"start_page {start_page} beyond last page {n_pages}")

    end_page = min(start_page + page_limit - 1, n_pages)
    window = list(range(start_page, end_page + 1))
    tail = [p for p in range(max(n_pages - tail_pages + 1, 1), n_pages + 1)
            if p > end_page] if tail_pages else []

    import pdfplumber
    from pdfminer.pdfdocument import PDFPasswordIncorrect
    try:
        pdf = pdfplumber.open(io.BytesIO(data), password="", pages=window + tail)
    except PDFPasswordIncorrect as exc:
        raise ToolError(FILE_ENCRYPTED, "PDF requires a user password") from exc
    except Exception as exc:
        raise ToolError(FILE_CORRUPT, f"cannot open PDF: {exc}") from exc

    pages_out, tail_out = [], []
    with pdf:
        for page in pdf.pages:
            pno = page.page_number
            try:
                entry = _page(page, reader.pages[pno - 1], pno, include_tables,
                              table_strategy, include_amounts)
            except ToolError:
                raise
            except Exception as exc:
                # One unreadable page does not sink the document.
                entry = {"page": pno, "locator": f"p{pno}", "error": FILE_CORRUPT,
                         "message": str(exc)[:200]}
            finally:
                page.close()                  # free the layout cache page by page
            (pages_out if pno <= end_page else tail_out).append(entry)

    meta = {}
    try:
        info = reader.metadata or {}
        meta = {k: _s(info.get(f"/{k.capitalize()}")) for k in ("producer", "creator")}
    except Exception:
        pass

    has_more = end_page < n_pages
    result = {
        **state,
        **meta,
        "page_count": n_pages,
        "page_inventory": _inventory(reader),
        "form_fields": _form_fields(reader),
        "embedded_files": _embedded_files(reader),
        "window": {"start_page": start_page, "page_limit": page_limit,
                   "returned_pages": len(pages_out), "has_more": has_more,
                   "next_start_page": end_page + 1 if has_more else None},
        "pages_without_text_layer": [p["page"] for p in pages_out + tail_out
                                     if not p.get("has_text_layer")],
        "pages": pages_out,
        "tail": tail_out,
    }
    return envelope(TOOL, data, filename, params, result)
