"""Evidence: each readable item -> a document of cited blocks.

A block is one cell or one line with a locator. The model is shown blocks
as ``[d2#Summary!B14] $12400`` and must cite them back; grounding then checks
the cited block really holds the value. Tables are kept as structure for
column-based extraction.

Locators (joined to the document's source prefix to make a CTR-13 source):
  CSV       B14
  Excel     Summary!B14, Summary!textbox:H10, Summary!H20:comment
  PDF       p1:L4 (line), p1:T1:R2C3 (table cell)
  Email     subject, L3 (body line)
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from extractor_tools import ToolError, extract_pdf_text, list_sheets, read_csv, read_sheet
from extractor_tools.common import a1

from .intake import Item, Submission


@dataclass
class Block:
    locator: str
    text: str
    value: Any = None          # typed value from the reader, when it typed one
    vtype: str = "string"
    group: str = ""            # sheet, "csv", page "p1", or table "p1:T1"
    row: int | None = None
    col: int | None = None


@dataclass
class Table:
    group: str
    header: list[Block]
    rows: list[list[Block]]


@dataclass
class Doc:
    doc_id: str
    source: str                # "file:inv.csv", "attachment:inv.xlsx", "body"
    name: str
    kind: str                  # csv | excel | pdf | email_body
    sha256: str
    blocks: list[Block] = field(default_factory=list)
    tables: list[Table] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    status: str = "read"       # read | failed
    reason: str | None = None

    def by_locator(self) -> dict[str, Block]:
        return {b.locator: b for b in self.blocks}


def display(cell: dict[str, Any]) -> str:
    t, v = cell.get("type"), cell.get("value")
    if t == "formula_uncached":
        return f"[formula {cell.get('formula')}, not calculated]"
    if t == "percent":
        return f"{(Decimal(str(v)) * 100).normalize():f}%"
    if t in ("integer", "decimal") and cell.get("currency_symbol"):
        return f"{cell['currency_symbol']}{v}"
    if t == "boolean":
        return "TRUE" if v else "FALSE"
    return "" if v is None else str(v)


# ------------------------------------------------------------------ readers

def _csv_doc(doc: Doc, item: Item, ev: dict[str, Any]) -> None:
    r = read_csv(item.data, item.name, row_limit=int(ev["rows"]), tail_rows=int(ev["tail_rows"]))["result"]
    header_row = r["header_row"]
    header = []
    if header_row:
        for col in r["columns"]:
            if col["name"].strip():
                header.append(Block(a1(header_row, col["index"]), col["name"].strip(),
                                    group="csv", row=header_row, col=col["index"]))
    doc.blocks.extend(header)
    rows = []
    for row in r["rows"] + r["tail"]:
        blocks = []
        for c in row["cells"]:
            col = _col_of(c["ref"])
            blocks.append(Block(c["ref"], c["raw"].strip(), c.get("value"), c["type"],
                                group="csv", row=row["row"], col=col))
        doc.blocks.extend(blocks)
        rows.append(blocks)
    if header:
        doc.tables.append(Table("csv", header, rows))
    if r["page"]["has_more"]:
        doc.notes.append(f"rows_truncated:{r['data_row_count']}")


def _col_of(ref: str) -> int:
    from extractor_tools.common import col_index
    letters = "".join(ch for ch in ref if ch.isalpha())
    return col_index(letters)


def _excel_doc(doc: Doc, item: Item, ev: dict[str, Any]) -> None:
    inv = list_sheets(item.data, item.name)["result"]
    sheets = [s for s in inv["sheets"] if s["kind"] == "worksheet" and s["state"] == "visible"
              and s.get("non_empty_cells")][: int(ev["max_sheets"])]
    skipped = [s["name"] for s in inv["sheets"] if s["kind"] == "worksheet" and s["state"] != "visible"]
    if skipped:
        doc.notes.append("hidden_sheets_skipped:" + ",".join(skipped))
    for s in sheets:
        name = s["name"]
        r = read_sheet(item.data, item.name, sheet=name, view="cells",
                       row_limit=int(ev["rows"]), tail_rows=int(ev["tail_rows"]))["result"]
        grid: list[list[Block]] = []
        for row in r["rows"] + r["tail"]:
            blocks = [Block(f"{name}!{c['ref']}", display(c), c.get("value"), c["type"],
                            group=name, row=row["row"], col=_col_of(c["ref"]))
                      for c in row["cells"] if not c.get("hidden")]
            if blocks:
                doc.blocks.extend(blocks)
                grid.append(blocks)
        out = r.get("outside_cells") or {}
        for tb in out.get("text_boxes", []):
            doc.blocks.append(Block(f"{name}!textbox:{tb['anchor']}", tb["text"], group=f"{name}:textbox"))
        for cm in out.get("comments", []):
            doc.blocks.append(Block(f"{name}!{cm['ref']}:comment", cm["text"], group=f"{name}:comment"))
        doc.tables.extend(_sheet_tables(name, grid))
        if r["page"] and r["page"]["has_more"]:
            doc.notes.append(f"rows_truncated:{name}:{r['row_count']}")


def _sheet_tables(group: str, grid: list[list[Block]]) -> list[Table]:
    """Header = a row of 2+ text cells followed by a row holding a number or date."""
    tables: list[Table] = []
    current: Table | None = None
    for i, row in enumerate(grid):
        is_header = (len(row) >= 2 and all(b.vtype == "string" for b in row)
                     and i + 1 < len(grid)
                     and any(b.vtype != "string" for b in grid[i + 1]))
        if is_header:
            current = Table(group, row, [])
            tables.append(current)
        elif current is not None:
            current.rows.append(row)
    return tables


def _pdf_doc(doc: Doc, item: Item, ev: dict[str, Any]) -> None:
    r = extract_pdf_text(item.data, item.name, page_limit=int(ev["pages"]),
                         tail_pages=int(ev["tail_pages"]))["result"]
    for page in r["pages"] + r["tail"]:
        if page.get("error"):
            doc.notes.append(f"page_unreadable:{page['page']}")
            continue
        for ln in page["lines"]:
            if ln.get("hidden"):
                doc.notes.append(f"hidden_text:{ln['locator']}")
                continue
            doc.blocks.append(Block(ln["locator"], ln["text"], group=f"p{page['page']}",
                                    row=ln["line"]))
        for t in page.get("tables", []):
            grid: list[list[Block]] = []
            for ri, row in enumerate(t["rows"], start=1):
                blocks = []
                for cell in row:
                    loc = cell["locator"]
                    col = int(loc.rsplit("C", 1)[1])
                    blocks.append(Block(loc, (cell["text"] or "").strip(), group=t["locator"],
                                        row=ri, col=col))
                doc.blocks.extend(blocks)
                grid.append(blocks)
            if len(grid) >= 2:
                doc.tables.append(Table(t["locator"], grid[0], grid[1:]))
    if r["pages_without_text_layer"]:
        doc.notes.append("no_text_layer:" + ",".join(f"p{p}" for p in r["pages_without_text_layer"]))
    if r["window"]["has_more"]:
        doc.notes.append(f"pages_truncated:{r['page_count']}")
    if not any(b.locator.count(":L") for b in doc.blocks):
        raise ToolError("no_text_layer", "PDF has no text layer on the pages read")


# ------------------------------------------------------------------ documents

def build_docs(sub: Submission, ev: dict[str, Any]) -> tuple[list[Doc], list[str]]:
    docs: list[Doc] = []
    reasons: list[str] = []
    if sub.subject or sub.body_text:
        body = Doc(f"d{len(docs) + 1}", "body", "email body", "email_body",
                   hashlib.sha256((sub.body_text or "").encode()).hexdigest())
        if sub.subject:
            body.blocks.append(Block("subject", sub.subject.strip(), group="subject"))
        for i, line in enumerate((sub.body_text or "").splitlines(), start=1):
            if line.strip():
                body.blocks.append(Block(f"L{i}", " ".join(line.split()), group="body", row=i))
        docs.append(body)
    for item in sub.items:
        doc = Doc(f"d{len(docs) + 1}", item.source_prefix, item.name, item.kind,
                  hashlib.sha256(item.data).hexdigest())
        try:
            {"csv": _csv_doc, "excel": _excel_doc, "pdf": _pdf_doc}[item.kind](doc, item, ev)
        except ToolError as exc:
            doc.status = "failed"
            doc.reason = "encrypted_no_key" if exc.code == "file_encrypted" \
                else f"attachment_parse_failed:{item.name}"
            doc.notes.append(f"{exc.code}: {exc}")
            doc.blocks, doc.tables = [], []
            reasons.append(doc.reason)
        docs.append(doc)
    return docs, reasons


def render(docs: list[Doc], max_chars: int) -> str:
    """Evidence text for the model: one line per row or text line, every cell cited."""
    parts = []
    for doc in docs:
        if doc.status != "read":
            continue
        lines = [f"### {doc.doc_id} · {doc.source} ({doc.kind})"]
        rows: dict[tuple[str, int | None], list[Block]] = {}
        order: list[tuple[str, int | None]] = []
        for b in doc.blocks:
            if ":T" in b.group:          # PDF table cells repeat the PDF lines
                continue
            key = (b.group, b.row) if b.row is not None and b.col is not None else (b.locator, None)
            if key not in rows:
                rows[key] = []
                order.append(key)
            rows[key].append(b)
        size = len(lines[0])
        for key in order:
            line = " | ".join(f"[{doc.doc_id}#{b.locator}] {b.text}" for b in rows[key])
            if size + len(line) > max_chars:
                lines.append(f"[{doc.doc_id}] … evidence truncated at {max_chars} characters")
                break
            lines.append(line)
            size += len(line) + 1
        parts.append("\n".join(lines))
    return "\n\n".join(parts)
