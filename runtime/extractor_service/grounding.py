"""Grounding: does the cited block really hold the extracted value?

Every value must be traceable to the evidence. A value found at its cited
block is ``verified``. A value found elsewhere is ``relocated`` (its source
is corrected). A value found nowhere is ``unverified``, and is dropped
unless its field allows inference (``grounding: optional``).
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any

from extractor_tools.values import parse_date, parse_number

from .evidence import Block, Doc
from .schema import Field, date_tokens

_NUM_TIGHT = re.compile(r"\d[\d,.']*\d|\d")
_NUM_SPACED = re.compile(r"\d[\d,.'  ]*\d")


def _norm(s: str) -> str:
    return " ".join(s.casefold().split())


def _numbers(text: str) -> set[Decimal]:
    out: set[Decimal] = set()
    for pattern in (_NUM_TIGHT, _NUM_SPACED):
        for tok in pattern.findall(text):
            for sep in (".", ","):
                p = parse_number(tok.replace(" ", " "), sep)
                if p and p["type"] in ("integer", "decimal"):
                    try:
                        out.add(abs(Decimal(str(p["value"]))))
                    except InvalidOperation:
                        pass
    return out


def _dates(text: str) -> set[str]:
    out: set[str] = set()
    for tok in date_tokens(text):
        for order in (None, "DMY", "MDY"):
            p = parse_date(tok, order)
            if not p:
                continue
            if p["type"] == "date_ambiguous":
                out.update(p["candidates"].values())
            elif p["value"]:
                out.add(p["value"][:10])
    return out


def block_holds(f: Field, value: Any, block: Block) -> bool:
    if value is None:
        return False
    if f.type in ("integer", "decimal"):
        target = abs(Decimal(value))
        if block.vtype in ("integer", "decimal") and block.value is not None:
            try:
                if abs(Decimal(str(block.value))) == target:
                    return True
            except InvalidOperation:
                pass
        return target in _numbers(block.text)
    if f.type == "date":
        if block.vtype in ("date", "datetime") and block.value:
            if str(block.value)[:10] == value:
                return True
        return value in _dates(block.text)
    if f.type == "boolean":
        return True
    return _norm(str(value)) in _norm(block.text) if str(value).strip() else False


def ground(f: Field, value: Any, docs: dict[str, Doc], citation: str) -> tuple[str, str | None]:
    """-> (status, "doc_id#locator"). Looks at the cited block, then its document, then all."""
    doc_id, _, locator = (citation or "").partition("#")
    doc = docs.get(doc_id)
    if doc is not None:
        block = doc.by_locator().get(locator)
        if block is not None and block_holds(f, value, block):
            return "verified", f"{doc_id}#{locator}"
        for b in doc.blocks:
            if block_holds(f, value, b):
                return "relocated", f"{doc_id}#{b.locator}"
    for d in docs.values():
        if d is doc:
            continue
        for b in d.blocks:
            if block_holds(f, value, b):
                return "relocated", f"{d.doc_id}#{b.locator}"
    return "unverified", None


def row_blocks(doc: Doc, locator: str) -> list[Block]:
    """Blocks in the same row as the cited block (or the block alone)."""
    block = doc.by_locator().get(locator)
    if block is None:
        return []
    if block.row is None or block.col is None:
        return [block]
    return [b for b in doc.blocks if b.group == block.group and b.row == block.row]
