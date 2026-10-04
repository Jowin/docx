"""Word Writer tool: an extraction result -> a .docx report a person reads.

The report: a title (use case and input), a run summary (status, flags,
confidence, config version, email type, audit id), then each record as a
field table (value, confidence, source) followed by its line items as a
table. A flagged record carries its flags in a highlighted line. Pure:
fixed document properties, so the same result gives the same report.
"""
from __future__ import annotations

import io
from datetime import datetime
from typing import Any

from docx import Document
from docx.shared import Pt, RGBColor

from .result_table import shape, text

TOOL = "docx_writer"
MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
EXTENSION = "docx"
_EPOCH = datetime(2000, 1, 1)
_WARN = RGBColor(0xB4, 0x53, 0x09)


def write_docx(result: Any, *, title: str | None = None) -> bytes:
    s = shape(result)
    doc = Document()
    doc.styles["Normal"].font.size = Pt(10)
    doc.add_heading(title or s.title, level=1)

    if s.extended:
        md = s.metadata
        cfg, cls = md.get("config") or {}, md.get("classification") or {}
        summary = [("Status", "flagged" if s.flagged else "clean"),
                   ("Flags", ", ".join(s.run_flags) or "none"),
                   ("Confidence", text(s.confidence)), ("Records", str(len(s.rows))),
                   ("Config", f"{cfg.get('client')}/{cfg.get('usecase')} {cfg.get('version')}" if cfg else "—"),
                   ("Email type", text(cls.get("type"))), ("Audit id", text(md.get("audit_id"))),
                   ("Processed at", text(md.get("processed_at")))]
        t = doc.add_table(rows=0, cols=2)
        t.style = "Light List"
        for k, v in summary:
            cells = t.add_row().cells
            cells[0].text, cells[1].text = k, v
            cells[0].paragraphs[0].runs[0].bold = True

    if not s.rows:
        doc.add_paragraph("No records were extracted.")
    for i, row in enumerate(s.rows, start=1):
        m = s.meta[i - 1]
        doc.add_heading(f"Record {i}", level=2)
        if m.flags:
            p = doc.add_paragraph()
            run = p.add_run("Flags: " + ", ".join(m.flags))
            run.font.color.rgb = _WARN
            run.bold = True
        cols = ["Field", "Value", "Confidence", "Source"] if s.extended else ["Field", "Value"]
        t = doc.add_table(rows=1, cols=len(cols))
        t.style = "Light Grid Accent 1"
        for c, name in zip(t.rows[0].cells, cols):
            c.text = name
        for name in s.columns:
            f = m.fields.get(name, {})
            values = [name, text(row[name])]
            if s.extended:
                values += [text(f.get("confidence")), text(f.get("source"))]
            for c, v in zip(t.add_row().cells, values):
                c.text = v
        for name, (icols, items) in s.arrays.items():
            mine = [it for it in items if it["_record"] == i]
            if not mine:
                continue
            doc.add_paragraph(name.replace("_", " ").capitalize(), style="Heading 3")
            t = doc.add_table(rows=1, cols=len(icols))
            t.style = "Light Grid Accent 1"
            for c, n in zip(t.rows[0].cells, icols):
                c.text = n
            for it in mine:
                for c, n in zip(t.add_row().cells, icols):
                    c.text = text(it.get(n))

    props = doc.core_properties
    props.created = props.modified = props.last_printed = _EPOCH
    props.author = props.last_modified_by = "DataExtractor"
    props.title = title or s.title
    props.revision = 1
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()
