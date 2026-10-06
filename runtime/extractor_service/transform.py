"""Transform: deterministic transformation and lookup over every record (ZEN rules).

After ``verify`` (every record, including the rows ``expand`` read) and before
``route``, each record goes through the rule sets that apply, in this order:

    1. the version's   rules/transform.decision.json      (+ rules/tables/**)
    2. global          lookups/transform.decision.json    (+ lookups/tables/**)
    3. client          <client>/lookups/...
    4. use case        <client>/<usecase>/lookups/...

The version's rules are logic released with the version (sign-off, rollback).
The lookup levels are reference data that changes without a new version (a
new account -> portfolio mapping, say), as the ingestion lookups do; the
narrowest level runs last, so it has the final word. Each level sees what the
level before it produced.

A rule set is a ZEN decision (extractor_tools/rules.py). It is given::

    {"record": {<field>: <value>, ...},                        the record's values
     "meta":   {"client", "usecase", "email_type", "sender", "sender_domain",
                "subject", "input", "document", "index"}}

and returns the same shape (pass-through nodes make that the default). What it
may change:

* ``record.<field>``: a field of the dictionary takes the new value. It is
  normalised and validated like an extracted value; an invalid one is
  ``schema_validation_failed:<field>``. The field keeps its source and gains
  ``transform: {"rule", "from"}``; a field the rules fill from nothing is
  ``grounding: "derived"``, with the confidence of the weakest value it could
  have been derived from. A key that is not a dictionary field is ignored and
  counted in ``metadata.transform.unknown_fields``.
* ``flags``: a string or list of strings, added to the record's flags as
  ``rule:<flag>`` (``portfolio_not_mapped``, say), so a rule can send a record
  to review without changing it.

Array fields (line items) are given to the rules but not changed by them.
A record a rule set fails on keeps its values from before that rule set and
is flagged ``transform_rule_error:<level>``; nothing is dropped. Required
fields are re-checked afterwards (``missing_field``).
"""
from __future__ import annotations

import re
from collections import Counter
from pathlib import Path
from typing import Any

from extractor_tools.common import ToolError
from extractor_tools.rules import RuleSet

from .errors import ConfigError
from .schema import normalize

ENTRY = "transform.decision.json"
_SAME = object()


def layers(cfg: Any) -> list[tuple[str, RuleSet]]:
    """The rule sets for a config, in the order they run (cached on the config)."""
    key = "_transform_layers"
    cached = cfg.__dict__.get(key)
    if cached is not None:
        return cached
    from .ingest_filter import folders
    root = cfg.root or cfg.path.parent.parent.parent
    places = [("version", Path(cfg.path) / "rules")] + \
        [(lvl, p) for lvl, p in reversed(folders(root, cfg.client, cfg.usecase))]
    out = []
    for level, folder in places:
        if (folder / ENTRY).is_file():
            try:
                out.append((level, RuleSet.from_folder(folder, ENTRY)))
            except ToolError as exc:
                raise ConfigError("config_invalid", f"{level} transform rules: {exc}",
                                  {"path": str(folder)}) from exc
    object.__setattr__(cfg, key, out)
    return out


def describe(cfg: Any) -> list[dict[str, Any]]:
    return [{"level": lvl, "path": rs.source, "decisions": sorted(rs.decisions), "sha256": rs.sha256}
            for lvl, rs in layers(cfg)]


def _meta(cfg: Any, sub: Any, docs: dict[str, Any], rec: dict[str, Any], index: int) -> dict[str, Any]:
    domain = ""
    sender = getattr(sub, "sender", None) or ""
    m = re.search(r"@([A-Za-z0-9.-]+)", sender)
    if m:
        domain = m.group(1).lower()
    doc_id = next((str(v.get("source") or "").partition("#")[0] for v in rec["fields"].values()
                   if isinstance(v, dict) and v.get("source")), "")
    doc = docs.get(doc_id)
    return {"client": cfg.client, "usecase": cfg.usecase, "email_type": cfg.email_type or "",
            "sender": sender, "sender_domain": domain, "subject": getattr(sub, "subject", None) or "",
            "input": getattr(sub, "name", ""), "document": getattr(doc, "name", "") if doc else "", "index": index}


def _values(fields: dict[str, Any]) -> dict[str, Any]:
    out = {}
    for name, v in fields.items():
        if isinstance(v, dict) and "items" in v:
            out[name] = [{k: (c or {}).get("value") for k, c in (item or {}).items()} for item in v["items"] or []]
        elif isinstance(v, dict):
            out[name] = v.get("value")
    return out


def apply(cfg: Any, records: list[dict[str, Any]], sub: Any, docs: list[Any]) -> tuple[list[dict[str, Any]],
                                                                                       dict[str, Any] | None]:
    """Run the rule sets over the records; returns (records, metadata.transform or None)."""
    sets = layers(cfg)
    if not sets or not records:
        return records, ({"layers": describe(cfg), "records_changed": 0, "changes": {}} if sets else None)
    fields = {f.name: f for f in cfg.dictionary.fields}
    by_id = {d.doc_id: d for d in docs}
    changes: Counter = Counter()
    unknown: Counter = Counter()
    errors: Counter = Counter()
    changed_records: set[int] = set()
    recs = [{"fields": dict(r["fields"]), "reasons": list(r["reasons"])} for r in records]
    metas = [_meta(cfg, sub, by_id, r, i) for i, r in enumerate(recs)]
    for level, rules in sets:
        results = rules.evaluate_many([{"record": _values(r["fields"]), "meta": metas[i]}
                                       for i, r in enumerate(recs)])
        for i, (res, err) in enumerate(results):
            rec = recs[i]
            if err is not None:
                rec["reasons"].append(f"transform_rule_error:{level}")
                errors[level] += 1
                continue
            new = res.get("record") if isinstance(res.get("record"), dict) else {}
            for name, value in new.items():
                f = fields.get(name)
                if f is None:
                    unknown[name] += 1
                    continue
                if f.type == "array":
                    continue
                old = rec["fields"].get(name) or {"value": None, "confidence": 0.0, "source": None}
                if value == old.get("value"):
                    continue
                if _changed(cfg, rec, f, old, value, level):
                    changes[name] += 1
                    changed_records.add(i)
            for flag in _flags(res.get("flags")):
                rec["reasons"].append(f"rule:{flag}")
    for rec in recs:
        missing = {f"missing_field:{f.name}" for f in cfg.dictionary.required
                   if (rec["fields"].get(f.name) or {}).get("value") is None}
        rec["reasons"] = [r for r in rec["reasons"] if not r.startswith("missing_field:") or r in missing] + \
            sorted(m for m in missing if m not in rec["reasons"])
        rec["reasons"] = list(dict.fromkeys(rec["reasons"]))
    report = {"layers": describe(cfg), "records_changed": len(changed_records), "changes": dict(changes)}
    if unknown:
        report["unknown_fields"] = dict(unknown)
    if errors:
        report["errors"] = dict(errors)
    return recs, report


def _changed(cfg: Any, rec: dict[str, Any], f: Any, old: dict[str, Any], value: Any, level: str) -> bool:
    entry = {k: v for k, v in old.items() if k not in ("value", "error")}
    prior = old.get("transform", {}).get("from", old.get("value")) if old.get("transform") else old.get("value")
    if value is None:
        entry.update(value=None, transform={"rule": level, "from": prior})
        rec["fields"][f.name] = entry
        return True
    norm, err = normalize(f, value, date_order=cfg.date_order)
    if norm is None:
        norm, err = value, err or "invalid"
    if norm == old.get("value") and not err:
        return False
    if old.get("value") is None:
        present = [v.get("confidence") or 0.0 for v in rec["fields"].values()
                   if isinstance(v, dict) and v.get("value") is not None]
        entry.update(confidence=round(min(present), 4) if present else 0.0, grounding="derived")
    entry.update(value=norm, transform={"rule": level, "from": prior})
    flag = f"schema_validation_failed:{f.name}"
    if err:
        entry["error"] = err
        rec["reasons"].append(flag)
    else:                                           # a rule that repairs a value clears its flag
        rec["reasons"] = [r for r in rec["reasons"] if r != flag]
    rec["fields"][f.name] = entry
    return True


def _flags(value: Any) -> list[str]:
    if not value:
        return []
    items = value if isinstance(value, list) else [value]
    return [re.sub(r"[^\w.:-]+", "_", str(v)).strip("_")[:80] for v in items if str(v).strip()]
