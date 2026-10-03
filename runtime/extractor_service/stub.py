"""Deterministic stub model: label and column matching from the data dictionary.

It stands behind the same interface as the model gateway path, so the whole
service runs offline and in tests with byte-identical results. It reads only
what the dictionary's aliases name:

  label in the block      "Invoice No: INV-20194", "Total Due $1,500.00"
  label, value to right   [Due Date] [2026-09-14]
  column header           CSV/Excel/PDF table column "Total Due" -> its value
  array fields            the table whose header matches 2+ item fields
  anchor in a line        "... please pay $1,200.00 by ..." for a field whose
                          learned anchors include "please pay"

Older messages in an email thread (segment > 0) and OCR'd text score lower
than the newest message and text-layer content.

Records. The answer is a list of records keyed by the dictionary's
``record_key``:
  * A table with a key column ("Invoice No") gives one record per key value;
    rows sharing a key are one record, and their item columns become its
    array field (line items). Labels elsewhere in that document ("Supplier:
    Acme") fill fields the table has no column for.
  * Any other document gives one partial record from its labels and tables.
  * Partial records with the same key merge (an email body and its attached
    invoice). A partial with no key joins the only keyed record when there is
    exactly one; with several, it cannot be placed and is reported.

When several fields' labels fit the start of a block, the longest label wins
("Invoice Date" belongs to invoice_date, not to invoice_number's "invoice").
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from extractor_tools.values import find_amounts

from .evidence import Block, Doc, Table
from .schema import DataDictionary, Field, first_date_token, normalize

_ID_HINTS = ("number", " no", "#", " id", "reference", "ref")
_TOTAL_WORDS = re.compile(r"(?<![a-z])(sub-?total|total|tax|vat|gst|balance|amount due)(?![a-z])", re.I)
_SYMBOL_ISO = {"$": "USD", "US$": "USD", "€": "EUR", "£": "GBP", "₹": "INR", "¥": "JPY",
               "C$": "CAD", "A$": "AUD"}


@dataclass
class _Partial:
    order: tuple
    cands: dict[str, list[dict[str, Any]]]
    items: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    key: str | None = None


class StubModel:
    provider = "stub"
    name = "deterministic-stub"

    def __init__(self, date_order: str | None = None) -> None:
        self.date_order = date_order

    # ---------------------------------------------------------- public

    def extract(self, dictionary: DataDictionary, docs: list[Doc], **_: Any) -> dict[str, Any]:
        scalars = [f for f in dictionary.fields if f.type != "array"]
        arrays = [f for f in dictionary.fields if f.type == "array"]
        key = dictionary.get(dictionary.record_key)
        self._field_cache = {f.name: f for f in scalars}
        labels = sorted(((a, f) for f in scalars for a in f.labels), key=lambda x: -len(x[0]))
        partials: list[_Partial] = []

        for di, doc in enumerate(docs):
            if doc.status != "read":
                continue
            maps = {id(t): self._column_map(t, labels) for t in doc.tables}
            keyed = [t for t in doc.tables if key.name in maps[id(t)]]
            skip = {b.locator for t in keyed for b in t.header + [x for row in t.rows for x in row]}
            cands = self._label_candidates(doc, di, labels, skip, scalars)
            for t in doc.tables:
                if t not in keyed:
                    self._columns(cands, doc, t, maps[id(t)], di)
            if keyed:
                for t in keyed:
                    partials.extend(self._row_records(doc, di, t, maps[id(t)], key, arrays, cands))
            else:
                items = {f.name: self._array_items(f, doc, exclude=keyed) for f in arrays}
                if any(cands.values()) or any(items.values()):
                    partials.append(_Partial(order=(doc.kind == "email_body", di, 0), cands=cands, items=items))

        records, notes = self._group(partials, key)
        return {"records": [self._choose(dictionary, r, docs) for r in records], "notes": notes}

    # ---------------------------------------------------------- candidates

    def _blank(self, scalars) -> dict[str, list[dict[str, Any]]]:
        return {f.name: [] for f in scalars}

    def _label_candidates(self, doc: Doc, di: int, labels, skip: set[str], scalars):
        cands = self._blank(scalars)
        header_locs = {b.locator for t in doc.tables for b in t.header}
        index = _row_index(doc)
        anchored = [(a.casefold(), f) for f in scalars for a in f.anchors]
        for bi, block in enumerate(doc.blocks):
            if block.locator in header_locs:
                continue
            # anchors are explicit learned phrases, so they also read inside key tables
            for anchor, f in anchored:
                raw = _after_anchor(f, block.text, anchor)
                if raw is not None:
                    self._add(cands, f, raw, doc, block,
                              0.88 - (0.1 if doc.kind == "email_body" else 0.0), di, bi)
            if block.locator in skip:
                continue
            hit = _match_label(block.text, labels)
            if not hit:
                continue
            f, rest, has_sep = hit
            raw = _value_from_text(f, rest) if rest else None
            score, at = (0.88 if has_sep else 0.65), block
            if raw is None and not rest.strip():
                nb = _right_neighbour(index, block, labels)
                if nb is not None:
                    raw, score, at = nb.value if nb.value is not None else nb.text, 0.82, nb
            if raw is None:
                continue
            if doc.kind == "email_body":
                score -= 0.1         # an attachment's own figure beats the covering note
            self._add(cands, f, raw, doc, at, score, di, bi)
        return cands

    def _add(self, candidates, f: Field, raw, doc: Doc, block: Block, score: float,
             di: int, bi: int) -> None:
        value, err = normalize(f, raw, date_order=self.date_order)
        if value is None or err:
            return
        if block.segment:                       # an older message in the thread counts for less
            score -= 0.05 * min(block.segment, 3)
        if block.confidence is not None:        # OCR'd text: scaled by the OCR engine's confidence
            score *= 0.85 + 0.15 * block.confidence
        score = round(score, 4)
        candidates[f.name].append({
            "value": raw if not isinstance(raw, (int, float)) else str(raw),
            "source": f"{doc.doc_id}#{block.locator}", "confidence": score,
            # attachments before the email body, then document order
            "_order": (doc.kind == "email_body", di, bi)})

    def _column_map(self, table: Table, labels) -> dict[str, int]:
        """Scalar field -> column, for header cells that are exactly a field's label."""
        out: dict[str, int] = {}
        for hb in table.header:
            hit = _match_label(hb.text, labels)
            if hit and not hit[1].strip() and hit[0].name not in out:
                out[hit[0].name] = hb.col
        return out

    def _columns(self, candidates, doc: Doc, table: Table, colmap: dict[str, int], di: int) -> None:
        for name, col in colmap.items():
            f = self._field_cache[name]
            cells = [b for row in table.rows for b in row if b.col == col]
            if f.type in ("decimal", "integer"):
                totals = [b for row in table.rows for b in row
                          if b.col == col and any(_TOTAL_WORDS.search(x.text) for x in row
                                                  if x.vtype == "string")]
                cells = totals or cells
            distinct = {str(b.value if b.value is not None else b.text) for b in cells}
            for b in cells[:1]:
                raw = b.value if b.value is not None else b.text
                # a header is as explicit a label as "Label: value" when the column agrees
                self._add(candidates, f, raw, doc, b, 0.85 if len(distinct) == 1 else 0.45,
                          di, 10_000 + (b.row or 0))

    def _row_records(self, doc: Doc, di: int, table: Table, colmap: dict[str, int], key: Field,
                     arrays: list[Field], doc_cands) -> list[_Partial]:
        """One partial record per key value in a table with a key column."""
        groups: dict[str, list[list[Block]]] = {}
        for row in table.rows:
            cell = next((b for b in row if b.col == colmap[key.name]), None)
            if cell is None or any(b.vtype == "string" and _TOTAL_WORDS.search(b.text) for b in row
                                   if b is not cell):
                continue
            value, err = normalize(key, cell.value if cell.value is not None else cell.text,
                                   date_order=self.date_order)
            if value is None or err:
                continue
            groups.setdefault(str(value).casefold(), []).append(row)

        item_maps = {f.name: self._item_map(f, table) for f in arrays}
        out = []
        for rows in groups.values():
            cands = {name: [] for name in doc_cands}
            for name, col in colmap.items():
                f = self._field_cache[name]
                cells = [b for row in rows for b in row if b.col == col]
                distinct = {str(b.value if b.value is not None else b.text) for b in cells}
                for b in cells[:1]:
                    raw = b.value if b.value is not None else b.text
                    self._add(cands, f, raw, doc, b, 0.85 if len(distinct) == 1 else 0.6, di, b.row or 0)
            for name, lst in doc_cands.items():          # document labels fill the rest
                if name not in colmap and name != key.name:
                    cands[name].extend(lst)
            items = {}
            for f in arrays:
                mapping = item_maps[f.name]
                items[f.name] = self._items_from_rows(doc, rows, mapping) if mapping else []
            out.append(_Partial(order=(False, di, rows[0][0].row or 0), cands=cands, items=items))
        return out

    # ---------------------------------------------------------- arrays

    def _item_map(self, f: Field, table: Table) -> dict[int, Field]:
        mapping: dict[int, Field] = {}
        for hb in table.header:
            h = hb.text.strip().strip(":#").casefold()
            for sub in f.items:
                if h in sub.labels and sub not in mapping.values():
                    mapping[hb.col] = sub
                    break
        return mapping if len(mapping) >= min(2, len(f.items)) else {}

    def _items_from_rows(self, doc: Doc, rows, mapping: dict[int, Field]) -> list[dict[str, Any]]:
        items = []
        for row in rows:
            if any(b.vtype == "string" and _TOTAL_WORDS.search(b.text) for b in row):
                continue
            values: dict[str, Any] = {}
            for b in row:
                sub = mapping.get(b.col)
                if sub is not None:
                    raw = b.value if b.value is not None else b.text
                    values[sub.name] = raw if not isinstance(raw, (int, float)) else str(raw)
            if values:
                items.append({"values": values, "source": f"{doc.doc_id}#{row[0].locator}",
                              "confidence": 0.75})
        return items

    def _array_items(self, f: Field, doc: Doc, exclude) -> list[dict[str, Any]]:
        best = None
        for table in doc.tables:
            if table in exclude:
                continue
            mapping = self._item_map(f, table)
            if mapping and (best is None or len(mapping) > len(best[1])):
                best = (table, mapping)
        return self._items_from_rows(doc, best[0].rows, best[1]) if best else []

    # ---------------------------------------------------------- records

    def _group(self, partials: list[_Partial], key: Field) -> tuple[list[_Partial], list[str]]:
        for p in partials:
            ranked = sorted(p.cands.get(key.name, []), key=lambda c: (-c["confidence"], c["_order"]))
            if ranked:
                v, _ = normalize(key, ranked[0]["value"], date_order=self.date_order)
                p.key = str(v).casefold() if v is not None else None
        groups: dict[str, _Partial] = {}
        for p in sorted((p for p in partials if p.key), key=lambda p: p.order):
            g = groups.get(p.key)
            if g is None:
                groups[p.key] = _Partial(order=p.order, cands={k: list(v) for k, v in p.cands.items()},
                                         items=dict(p.items), key=p.key)
            else:
                _merge_into(g, p)
        records = sorted(groups.values(), key=lambda g: g.order)
        loose = [p for p in partials if not p.key]
        notes: list[str] = []
        if loose:
            if len(records) == 1:
                for p in loose:
                    _merge_into(records[0], p)
            elif not records:
                head = _Partial(order=loose[0].order, cands={k: list(v) for k, v in loose[0].cands.items()},
                                items=dict(loose[0].items))
                for p in loose[1:]:
                    _merge_into(head, p)
                records = [head]
            else:
                notes.append(f"unplaced_partials:{len(loose)}")
        return records, notes

    def _choose(self, dictionary: DataDictionary, rec: _Partial, docs: list[Doc]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        # currency is inferred from the total's symbol, so it is decided last
        ordered = sorted(dictionary.fields, key=lambda f: (f.type == "array", f.name == "currency"))
        for f in ordered:
            if f.type == "array":
                out[f.name] = {"items": rec.items.get(f.name, [])}
                continue
            cands = sorted(rec.cands.get(f.name, []), key=lambda c: (-c["confidence"], c["_order"]))
            if f.name == "currency" and not cands:
                cands = self._currency(out, docs)
            if cands:
                best = cands[0]
                out[f.name] = {"value": best["value"], "source": best["source"],
                               "confidence": best["confidence"],
                               "candidates": [{k: c[k] for k in ("value", "source", "confidence")}
                                              for c in cands]}
            else:
                out[f.name] = {"value": None, "source": "", "confidence": 0.0}
        return {f.name: out[f.name] for f in dictionary.fields}

    def _currency(self, out: dict[str, Any], docs: list[Doc]) -> list[dict[str, Any]]:
        total = out.get("total_amount") or {}
        doc_id, _, loc = (total.get("source") or "").partition("#")
        for doc in docs:
            if doc.doc_id != doc_id:
                continue
            block = doc.by_locator().get(loc)
            if block is None:
                return []
            amounts = find_amounts(block.text)
            if amounts:
                cur = amounts[0]["currency"]
                iso = cur if len(cur) == 3 and cur.isalpha() else _SYMBOL_ISO.get(cur)
                if iso:
                    return [{"value": iso, "source": total["source"],
                             "confidence": 0.7 if iso == cur else 0.6, "_order": (0,)}]
        return []


def _merge_into(g: _Partial, p: _Partial) -> None:
    for k, v in p.cands.items():
        g.cands.setdefault(k, []).extend(v)
    for k, v in p.items.items():
        if v and not g.items.get(k):
            g.items[k] = v
    g.order = min(g.order, p.order)


def _row_index(doc: Doc) -> dict[tuple[str, int], list[Block]]:
    index: dict[tuple[str, int], list[Block]] = {}
    for b in doc.blocks:
        if b.row is not None and b.col is not None:
            index.setdefault((b.group, b.row), []).append(b)
    for row in index.values():
        row.sort(key=lambda b: b.col)
    return index


def _right_neighbour(index, block: Block, labels) -> Block | None:
    if block.row is None or block.col is None:
        return None
    for b in index.get((block.group, block.row), []):
        if b.col > block.col and b.text.strip():
            if b.vtype == "string" and _match_label(b.text, labels):
                return None              # the next cell is another label
            return b
    return None


def _match_label(text: str, labels) -> tuple[Field, str, bool] | None:
    t = text.strip()
    low = t.casefold()
    for alias, f in labels:                      # longest alias first
        nxt = low[len(alias)] if len(low) > len(alias) else ""
        # a label ends at a space or a separator; "PO-7700" is a value, not the label "PO"
        if low.startswith(alias) and (not nxt or nxt.isspace() or nxt in ":#="):
            rest = t[len(alias):]
            m = re.match(r"\s*(?:no\.?|number)?\s*([:#=]|\s[\-–]\s)?\s*", rest, re.I)
            has_sep = bool(m and m.group(1))
            return f, rest[m.end():] if m else rest, has_sep
    return None


def _after_anchor(f: Field, text: str, anchor: str) -> Any:
    """The value just after a learned anchor phrase, wherever it sits in the line."""
    at = text.casefold().find(anchor)
    if at < 0:
        return None
    end = at + len(anchor)
    if end < len(text) and text[end].isalnum() and anchor[-1:].isalnum():
        return None                              # "ref" must not anchor inside "reference"
    rest = re.sub(r"^\s*(?:[:#=]|\s[\-–]\s)?\s*", "", text[end:])
    return _value_from_text(f, rest) if rest else None


def _value_from_text(f: Field, rest: str) -> Any:
    rest = rest.split(" | ")[0].strip()
    if not rest:
        return None
    if f.type in ("decimal", "integer"):
        amounts = find_amounts(rest)
        if amounts:
            return amounts[0]["value"]
        m = re.search(r"-?\d[\d,.']*", rest)
        return m.group(0) if m else None
    if f.type == "date":
        return first_date_token(rest)
    if f.type == "string":
        id_like = any(h in f" {f.name.replace('_', ' ')}" for h in _ID_HINTS) or \
            any(any(h.strip() in a for h in _ID_HINTS) for a in f.aliases)
        if id_like:
            return rest.split()[0].strip(".,;")
        return re.split(r"\s{2,}|;|\|", rest)[0].strip(" .,")
    return rest
