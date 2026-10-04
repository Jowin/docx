"""PDF Writer tool: an extraction result -> a PDF report.

Same content as the Word report: title, run summary, each record's field
table (value, confidence, source) and its line items, flags highlighted.
Uses DejaVu Sans when the system has it (currency symbols, accents), else
Helvetica. Pure: ReportLab's invariant mode, so the same result gives the
same bytes.
"""
from __future__ import annotations

import io
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from .result_table import shape, text

TOOL = "pdf_writer"
MEDIA_TYPE = "application/pdf"
EXTENSION = "pdf"
_FONT_DIRS = ("/usr/share/fonts/truetype/dejavu", "/usr/share/fonts/dejavu", "C:/Windows/Fonts")
_font_cache: tuple[str, str] | None = None


def _fonts() -> tuple[str, str]:
    global _font_cache
    if _font_cache is None:
        _font_cache = ("Helvetica", "Helvetica-Bold")
        for d in _FONT_DIRS:
            reg, bold = Path(d) / "DejaVuSans.ttf", Path(d) / "DejaVuSans-Bold.ttf"
            if reg.is_file() and bold.is_file():
                pdfmetrics.registerFont(TTFont("DXSans", str(reg)))
                pdfmetrics.registerFont(TTFont("DXSans-Bold", str(bold)))
                _font_cache = ("DXSans", "DXSans-Bold")
                break
    return _font_cache


def write_pdf(result: Any, *, title: str | None = None) -> bytes:
    s = shape(result)
    font, bold = _fonts()
    styles = getSampleStyleSheet()
    for st in styles.byName.values():
        st.fontName = bold if st.name.startswith("Heading") or st.name == "Title" else font
    body = styles["BodyText"]
    small = styles["BodyText"].clone("small", fontSize=8, leading=10)
    warn = styles["BodyText"].clone("warn", textColor=colors.HexColor("#B45309"), fontName=bold)

    def p(v: Any, st=small) -> Paragraph:
        return Paragraph(escape(v if isinstance(v, str) else text(v)), st)

    def table(rows: list[list[Any]], widths: list[float] | None = None) -> Table:
        t = Table([[p(c) for c in r] for r in rows], colWidths=widths, repeatRows=1)
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E6EEFE")),
            ("FONTNAME", (0, 0), (-1, 0), bold),
            ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#C9CED6")),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ]))
        return t

    wide = any(len(cols) > 6 for cols, _ in s.arrays.values())
    size = landscape(A4) if wide else A4
    usable = size[0] - 30 * mm
    story: list[Any] = [Paragraph(escape(title or s.title), styles["Title"])]
    if s.extended:
        md = s.metadata
        cfg, cls = md.get("config") or {}, md.get("classification") or {}
        story.append(table([["Status", "Flags", "Confidence", "Config", "Email type", "Audit id"],
                            ["flagged" if s.flagged else "clean", ", ".join(s.run_flags) or "none",
                             s.confidence, f"{cfg.get('client')}/{cfg.get('usecase')} {cfg.get('version')}"
                             if cfg else "—", cls.get("type"), md.get("audit_id")]]))
    if not s.rows:
        story += [Spacer(1, 4 * mm), Paragraph("No records were extracted.", body)]
    for i, row in enumerate(s.rows, start=1):
        m = s.meta[i - 1]
        story += [Spacer(1, 5 * mm), Paragraph(f"Record {i}", styles["Heading2"])]
        if m.flags:
            story.append(Paragraph(escape("Flags: " + ", ".join(m.flags)), warn))
        if s.extended:
            rows = [["Field", "Value", "Confidence", "Source"]] + [
                [n, row[n], m.fields.get(n, {}).get("confidence"), m.fields.get(n, {}).get("source")]
                for n in s.columns]
            widths = [usable * w for w in (0.22, 0.33, 0.12, 0.33)]
        else:
            rows = [["Field", "Value"]] + [[n, row[n]] for n in s.columns]
            widths = [usable * 0.35, usable * 0.65]
        story.append(table(rows, widths))
        for name, (icols, items) in s.arrays.items():
            mine = [it for it in items if it["_record"] == i]
            if mine:
                story += [Spacer(1, 3 * mm), Paragraph(escape(name.replace("_", " ").capitalize()),
                                                       styles["Heading3"])]
                story.append(table([icols] + [[it.get(c) for c in icols] for it in mine],
                                   [usable / len(icols)] * len(icols)))
    buf = io.BytesIO()
    SimpleDocTemplate(buf, pagesize=size, leftMargin=15 * mm, rightMargin=15 * mm, topMargin=15 * mm,
                      bottomMargin=15 * mm, title=title or s.title, author="DataExtractor",
                      invariant=1).build(story)
    return buf.getvalue()
