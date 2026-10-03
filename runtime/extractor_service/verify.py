"""Verification: check every extracted value against the evidence and score it.

Confidence per field (documented so reviewers can reason about it):
  model confidence          the model's (or stub's) own score, 0..1
  x grounding               1.0 verified at the cited block, 0.8 relocated,
                            0.6 inferred (not written, and the field allows it)
  x validation              0.5 when the value breaks its type, pattern or values
  + 0.05 agreement          when another source holds the same value (cap 1.0)
A value that cannot be found in the evidence and whose field requires
grounding is dropped and reported as ``unverified_value:<field>``.
"""
from __future__ import annotations

from typing import Any

from .config_store import ExtractionConfig
from .evidence import Doc
from .grounding import block_holds, ground, row_blocks
from .schema import Field, normalize, to_json

def finalize(cfg: ExtractionConfig, raw: dict[str, Any], docs: dict[str, Doc]
              ) -> tuple[dict[str, dict[str, Any]], list[str]]:
    reasons: list[str] = []
    out: dict[str, dict[str, Any]] = {}
    for f in cfg.dictionary.fields:
        r = raw.get(f.name) or {}
        if f.type == "array":
            out[f.name] = _finalize_array(cfg, f, r.get("items") or [], docs, reasons)
            continue
        value, err = normalize(f, r.get("value"), date_order=cfg.date_order)
        entry: dict[str, Any] = {"value": None, "confidence": 0.0, "source": None}
        if value is None:
            if f.required:
                reasons.append(f"missing_field:{f.name}")
            out[f.name] = entry
            continue
        status, cite = ground(f, value, docs, r.get("source", ""))
        if status == "unverified" and f.grounding == "required":
            reasons.append(f"unverified_value:{f.name}")
            if f.required:
                reasons.append(f"missing_field:{f.name}")
            out[f.name] = {**entry, "rejected": {"value": str(r.get("value")),
                                                 "cited": r.get("source"), "why": "not in evidence"}}
            continue
        if status == "unverified":           # allowed: the field may be inferred
            status = "inferred"
            doc_id, _, loc = str(r.get("source") or "").partition("#")
            cite = r["source"] if doc_id in docs and loc in docs[doc_id].by_locator() else None
        factor = {"verified": 1.0, "relocated": 0.8, "inferred": 0.6}[status]
        conf = float(r.get("confidence") or 0.0) * factor
        if err:
            conf *= 0.5
            reasons.append(f"schema_validation_failed:{f.name}")
        agree, conflict = _agreement(cfg, f, value, cite, r.get("candidates") or [], docs)
        if agree:
            conf = min(conf + 0.05, 1.0)
        if conflict:
            if f.critical:
                reasons.append(f"critical_field_conflict:{f.name}")
            conf = min(conf, 0.6)
        entry.update(value=value, confidence=round(conf, 4), source=cite, grounding=status,
                     **({"error": err} if err else {}),
                     **({"conflicts_with": conflict} if conflict else {}))
        out[f.name] = entry
    return out, reasons


def _agreement(cfg, f: Field, value: Any, cite: str | None, candidates: list[dict[str, Any]],
               docs: dict[str, Doc]) -> tuple[bool, list[dict[str, Any]]]:
    """Agreement: another candidate (different block) with the same value.
    Conflict: a strong candidate (>= 0.75) from another document with a different value."""
    agree, conflict = False, []
    cite_doc = (cite or "").partition("#")[0]
    for c in candidates:
        v, err = normalize(f, c.get("value"), date_order=cfg.date_order)
        if v is None or err or c.get("source") == cite:
            continue
        if v == value:
            agree = True
        elif c.get("confidence", 0) >= 0.75 and c.get("source", "").partition("#")[0] != cite_doc:
            conflict.append({"value": str(v), "source": c.get("source")})
    return agree, conflict


def _finalize_array(cfg, f: Field, items: list[dict[str, Any]], docs: dict[str, Doc],
                    reasons: list[str]) -> dict[str, Any]:
    rows, confs, rejected = [], [], 0
    for item in items:
        values = item.get("values") or {}
        doc_id, _, loc = str(item.get("source") or "").partition("#")
        doc = docs.get(doc_id)
        scope = row_blocks(doc, loc) if doc else []
        scope = scope or (doc.blocks if doc else [])
        obj: dict[str, Any] = {}
        checked = found = 0
        bad = False
        for sub in f.items:
            v, err = normalize(sub, values.get(sub.name), date_order=cfg.date_order)
            obj[sub.name] = v
            if v is None:
                continue
            bad = bad or bool(err)
            if sub.grounding == "required" and sub.type != "boolean":
                checked += 1
                found += any(block_holds(sub, v, b) for b in scope)
        if checked and found / checked < 0.5:
            rejected += 1
            continue
        ratio = found / checked if checked else 1.0
        conf = float(item.get("confidence") or 0.0) * (0.5 + 0.5 * ratio) * (0.5 if bad else 1.0)
        rows.append({"value": obj, "source": f"{doc_id}#{loc}" if doc else None, "confidence": round(conf, 4)})
        confs.append(conf)
    if rejected:
        reasons.append(f"unverified_value:{f.name}")
    if f.required and not rows:
        reasons.append(f"missing_field:{f.name}")
    return {"value": [r["value"] for r in rows] if rows or not f.required else None,
            "confidence": round(min(confs), 4) if confs else 0.0,
            "items": rows, **({"rejected_items": rejected} if rejected else {})}


def field_out(v: dict[str, Any], docs: dict[str, Doc], df: str) -> dict[str, Any]:
    out = {k: to_json(x, df) for k, x in v.items() if k not in ("source", "items")}
    if v.get("source"):
        out["source"] = _source_string(v["source"], docs)
    if "items" in v:
        out["items"] = [{"value": to_json(i["value"], df), "confidence": i["confidence"],
                         "source": _source_string(i["source"], docs) if i["source"] else None}
                        for i in v["items"]]
    return out


def _source_string(cite: str, docs: dict[str, Doc]) -> str:
    """"d2#Summary!B14" -> "attachment:inv.xlsx#Summary!B14" (CTR-13 form)."""
    doc_id, _, loc = cite.partition("#")
    doc = docs.get(doc_id)
    return f"{doc.source}#{loc}" if doc else cite


def dedupe(xs: list[str]) -> list[str]:
    seen, out = set(), []
    for x in xs:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out
