"""A long settlement report PDF with known contents, and a Textract stand-in that reads it.

``settlement_report(pages)`` builds a report the way custodians send them:

* a cover page (portfolio and period as labelled text) and a summary page with a
  small by-currency table that is *not* settlement rows;
* settlement tables of 25 rows per page, the header repeated on most pages and
  missing on every 7th (a continuation), each page with a "Page n of N" footer;
* every 25th table page is a scan (an image with no text layer), so its rows can
  only be read by OCR;
* a notes page in the middle, and a closing page with a grand-total table.

It returns the PDF bytes, the expected records in page order, and the ground
truth of every scanned page. ``FakeTextract`` answers ``analyze_document`` for
those scans with Textract-shaped Blocks (LINE, WORD, TABLE, CELL), identifying
the page by the hash of the image it is sent; ``errors`` misreads chosen cells
at low confidence, as a real engine sometimes does.

This is not Textract: it proves the pipeline end to end (rendering, the API
contract, parsing, table mapping, flags), not OCR accuracy.
"""
from __future__ import annotations

import hashlib
import io
import random
import uuid
from datetime import date, timedelta
from typing import Any

HEADER = ["Settlement Date", "Trade Date", "Security ID", "Transaction Type", "CCY", "Net Amount",
          "Portfolio", "Purpose Code", "Comments"]
FIELD_OF = {"Settlement Date": "settlement_date", "Trade Date": "trade_date", "Security ID": "security_id",
            "Transaction Type": "transaction_type", "CCY": "currency", "Net Amount": "amount",
            "Portfolio": "portfolio", "Purpose Code": "cash_purpose_code", "Comments": "comments"}
ROWS_PER_PAGE = 25
TYPES = ["BUY", "SELL", "DVP", "RVP", "CASH IN", "CASH OUT"]
CCYS = ["USD", "EUR", "GBP", "JPY", "CHF"]
PURPOSES = ["SECU", "TREA", "INTC", "DIVI"]


def _rows(rnd: random.Random, n: int, start: int) -> list[list[str]]:
    out = []
    base = date(2026, 9, 1)
    for i in range(start, start + n):
        trade = base + timedelta(days=i % 28)
        settle = trade + timedelta(days=2)
        isin = "US" + f"{rnd.randrange(10**9):09d}" + str(i % 10)
        amount = rnd.randrange(-5_000_000_00, 5_000_000_00) / 100
        out.append([settle.isoformat(), trade.isoformat(), isin, rnd.choice(TYPES), rnd.choice(CCYS),
                    f"{amount:,.2f}", f"PF-{i % 7:02d}", rnd.choice(PURPOSES), f"instr {i:05d}"])
    return out


def _expected(row: list[str]) -> dict[str, Any]:
    rec = {FIELD_OF[h]: v for h, v in zip(HEADER, row)}
    rec["amount"] = float(rec["amount"].replace(",", ""))
    if rec["amount"] == int(rec["amount"]):
        rec["amount"] = int(rec["amount"])
    return rec


def _draw_table(c, rows: list[list[str]], header: bool, top: float) -> None:
    from reportlab.lib import colors
    from reportlab.platypus import Table, TableStyle
    data = ([HEADER] if header else []) + rows
    t = Table(data, colWidths=[62, 56, 78, 62, 30, 72, 44, 56, 62], rowHeights=18)
    t.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.5, colors.black),
                           ("FONTSIZE", (0, 0), (-1, -1), 6.5),
                           ("FONTNAME", (0, 0), (-1, 0 if header else -1), "Helvetica-Bold" if header else "Helvetica"),
                           ("VALIGN", (0, 0), (-1, -1), "MIDDLE")]))
    _, h = t.wrap(0, 0)
    t.drawOn(c, 30, top - h)


def _table_page_pdf(rows: list[list[str]], header: bool, pno: int, total: int) -> bytes:
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    c.setFont("Helvetica-Bold", 10)
    c.drawString(30, 800, "Settlement instructions (continued)")
    _draw_table(c, rows, header, 780)
    c.setFont("Helvetica", 7)
    c.drawString(270, 30, f"Page {pno} of {total}")
    c.showPage()
    c.save()
    return buf.getvalue()


def _scan(pdf_page: bytes, dpi: int = 150) -> bytes:
    """The page as a scanner would deliver it: a PNG, slightly rotated noise-free image."""
    import pypdfium2 as pdfium
    doc = pdfium.PdfDocument(pdf_page)
    img = doc[0].render(scale=dpi / 72).to_pil().convert("L")
    doc.close()
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def settlement_report(pages: int = 500, *, scan_every: int = 25, seed: int = 7) -> dict[str, Any]:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas
    rnd = random.Random(seed)
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    expected: list[dict[str, Any]] = []
    scanned: dict[int, dict[str, Any]] = {}
    notes_page = pages // 2
    table_pages = [p for p in range(3, pages) if p != notes_page]   # last page: closing
    n = 0
    # 1: cover
    c.setFont("Helvetica-Bold", 16)
    c.drawString(60, 760, "Global Custody - Settlement Instruction Report")
    c.setFont("Helvetica", 11)
    for i, line in enumerate(["Client: Example Asset Management", "Period: September 2026",
                              f"Pages: {pages}", "Prepared by: Custody Operations"]):
        c.drawString(60, 720 - 18 * i, line)
    c.showPage()
    # 2: summary with a by-currency table (not settlement rows)
    c.setFont("Helvetica-Bold", 12)
    c.drawString(40, 800, "Summary by currency")
    from reportlab.lib import colors
    from reportlab.platypus import Table, TableStyle
    t = Table([["Currency", "Instructions", "Net Amount"]] + [[k, str(100 + i), f"{(i + 1) * 1234567.89:,.2f}"]
                                                            for i, k in enumerate(CCYS)], rowHeights=18)
    t.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.5, colors.black), ("FONTSIZE", (0, 0), (-1, -1), 8)]))
    _, h = t.wrap(0, 0)
    t.drawOn(c, 40, 780 - h)
    c.showPage()
    for p in range(3, pages + 1):
        if p == notes_page:
            c.setFont("Helvetica", 10)
            for i, line in enumerate(["Notes", "Amounts are net of fees. Negative amounts are payments.",
                                      "Instructions marked CASH IN/OUT carry no security identifier."]):
                c.drawString(40, 790 - 16 * i, line)
            c.showPage()
            continue
        if p == pages:
            c.setFont("Helvetica-Bold", 12)
            c.drawString(40, 800, "Closing balances")
            _draw_table(c, [["Grand Total", "", "", "", "", f"{sum(r['amount'] for r in expected):,.2f}",
                             "", "", ""]], True, 780)
            c.setFont("Helvetica", 9)
            c.drawString(40, 600, "End of report.")
            c.showPage()
            continue
        rows = _rows(rnd, ROWS_PER_PAGE, n)
        n += ROWS_PER_PAGE
        header = table_pages.index(p) % 7 != 6
        idx = table_pages.index(p)
        if idx % scan_every == scan_every - 1:
            png = _scan(_table_page_pdf(rows, header, p, pages))
            c.drawImage(ImageReader(io.BytesIO(png)), 0, 0, width=A4[0], height=A4[1])
            scanned[p] = {"rows": rows, "header": header}
        else:
            c.setFont("Helvetica-Bold", 10)
            c.drawString(30, 800, "Settlement instructions" + ("" if p == 3 else " (continued)"))
            _draw_table(c, rows, header, 780)
            c.setFont("Helvetica", 7)
            c.drawString(270, 30, f"Page {p} of {pages}")
        c.showPage()
        expected.extend(_expected(r) for r in rows)
    c.save()
    return {"pdf": buf.getvalue(), "expected": expected, "scanned": scanned, "pages": pages}


# ---------------------------------------------------------------------- the Textract stand-in


def _bbox(left: float, top: float, w: float, h: float) -> dict[str, Any]:
    return {"BoundingBox": {"Left": left, "Top": top, "Width": w, "Height": h}}


def textract_blocks(rows: list[list[str]], header: bool, *, conf=lambda r, c: 99.0,
                    text=lambda r, c, v: v, page: int = 1) -> list[dict[str, Any]]:
    """Textract AnalyzeDocument Blocks for one page holding one table (and a title line)."""
    blocks: list[dict[str, Any]] = [{"BlockType": "PAGE", "Id": str(uuid.uuid4()), "Page": page}]
    title = {"BlockType": "LINE", "Id": str(uuid.uuid4()), "Text": "Settlement instructions (continued)",
             "Confidence": 99.5, "Page": page, "Geometry": _bbox(0.05, 0.03, 0.4, 0.015)}
    blocks.append(title)
    grid = ([HEADER] if header else []) + rows
    table = {"BlockType": "TABLE", "Id": str(uuid.uuid4()), "Page": page, "Relationships": [{"Type": "CHILD", "Ids": []}],
             "Geometry": _bbox(0.05, 0.06, 0.9, 0.02 * len(grid))}
    blocks.append(table)
    for ri, row in enumerate(grid, start=1):
        top = 0.06 + 0.02 * (ri - 1)
        line_words = []
        for ci, value in enumerate(row, start=1):
            v = text(ri, ci, value)
            cell = {"BlockType": "CELL", "Id": str(uuid.uuid4()), "RowIndex": ri, "ColumnIndex": ci, "Page": page,
                    "Confidence": conf(ri, ci), "Geometry": _bbox(0.05 + 0.1 * (ci - 1), top, 0.1, 0.02)}
            ids = []
            for wi, word in enumerate(v.split()):
                w = {"BlockType": "WORD", "Id": str(uuid.uuid4()), "Text": word, "Confidence": conf(ri, ci),
                     "Page": page, "Geometry": _bbox(0.05 + 0.1 * (ci - 1) + 0.02 * wi, top, 0.02, 0.015)}
                blocks.append(w)
                ids.append(w["Id"])
                line_words.append(word)
            if ids:
                cell["Relationships"] = [{"Type": "CHILD", "Ids": ids}]
            blocks.append(cell)
            table["Relationships"][0]["Ids"].append(cell["Id"])
        blocks.append({"BlockType": "LINE", "Id": str(uuid.uuid4()), "Text": " ".join(line_words),
                       "Confidence": 98.0, "Page": page, "Geometry": _bbox(0.05, top, 0.9, 0.015)})
    return blocks


class FakeTextract:
    """``analyze_document`` for the scanned pages of a ``settlement_report``.

    ``errors``: {(page, data_row_index, header_name): misread_text} - shown with 41% confidence.
    """

    def __init__(self, report: dict[str, Any], errors: dict[tuple[int, int, str], str] | None = None,
                 fail_pages: set[int] | None = None):
        from extractor_service import ocr
        self.by_hash: dict[str, int] = {}
        self.report = report
        self.errors = errors or {}
        self.fail_pages = fail_pages or set()
        self.calls = 0
        for p in report["scanned"]:
            png = ocr.render_page(report["pdf"], p)
            self.by_hash[hashlib.sha256(png).hexdigest()] = p
            if len(png) > ocr.SYNC_MAX_BYTES:
                self.by_hash[hashlib.sha256(ocr._shrink(png)).hexdigest()] = p

    def analyze_document(self, *, Document: dict[str, Any], FeatureTypes: list[str]) -> dict[str, Any]:
        assert FeatureTypes == ["TABLES"]
        self.calls += 1
        p = self.by_hash.get(hashlib.sha256(Document["Bytes"]).hexdigest())
        if p is None:
            raise RuntimeError("UnsupportedDocumentException: page not known to the fake")
        if p in self.fail_pages:
            raise RuntimeError("ProvisionedThroughputExceededException")
        page = self.report["scanned"][p]
        off = 1 if page["header"] else 0

        def text(ri, ci, v):
            return self.errors.get((p, ri - off - 1, HEADER[ci - 1]), v)

        def conf(ri, ci):
            return 41.0 if (p, ri - off - 1, HEADER[ci - 1]) in self.errors else 97.5
        return {"Blocks": textract_blocks(page["rows"], page["header"], conf=conf, text=text),
                "DocumentMetadata": {"Pages": 1}}
