"""Email-type detection: the config's ``rules/detection.json`` (CTR-08), run before extraction.

The rules are what design-time's Detection Rules agent writes, one entry per
email type::

    {"schema_version": "1.0", "generated_by": "...",
     "types": {
       "invoice": {
         "keywords": [{"term": "invoice", "weight": 0.3}],
         "patterns": [{"name": "reference_no", "regex": "(?i)\\binv...", "weight": 0.2}],
         "entity_weights": {"MONEY": 0.25, "DATE": 0.1, "ORG": 0.1},
         "classification_threshold": 0.6,
         "negative_signals": ["quotation", "not a request for payment"]}}}

A type's score is the sum of the weights of its keywords found in the text
(case-insensitive), its patterns that match, and its entities present (MONEY
and DATE are recognised; ORG needs entity recognition the runtime does not
do, so it never scores), less 0.2 for every negative signal present. A type
*matches* when its score reaches its threshold.

The outcome, recorded in ``metadata.classification``:

* ``matched``      one type matched (or one clearly ahead of the rest);
* ``ambiguous``    two matched within ``classification.ambiguity_margin`` of
                   each other: extract as the higher, flag ``classification_ambiguous``;
* ``out_of_scope`` nothing matched, or the match is a type this config does
                   not extract: flag ``out_of_scope`` and, unless the manifest
                   says ``"classification": {"out_of_scope": "extract"}``, skip
                   extraction (an empty result, never a guess);
* ``unclassified`` the config has no detection rules: nothing is checked.
"""
from __future__ import annotations

import functools
import re
from typing import Any

from .errors import ConfigError

NEGATIVE_PENALTY = 0.2
_ENTITIES = {
    "MONEY": re.compile(r"(?i)(?:[$£€¥]\s?\d[\d,]*(?:\.\d{1,2})?|\b(?:usd|eur|gbp|inr|aud|cad|jpy)\s?\d[\d,]*"
                        r"(?:\.\d{1,2})?|\b\d[\d,]*\.\d{2}\b)"),
    "DATE": re.compile(r"(?i)\b(?:\d{4}-\d{2}-\d{2}|\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}|\d{1,2}\s+(?:jan|feb|mar|apr|"
                       r"may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d{4})\b"),
}


@functools.lru_cache(maxsize=512)
def _compiled(regex: str) -> re.Pattern[str]:
    return re.compile(regex)


def validate(data: Any) -> dict[str, Any]:
    """Check detection.json (patterns must compile); returns plain data, safe to checkpoint."""
    where = "rules/detection.json"
    if not isinstance(data, dict) or not isinstance(data.get("types"), dict) or not data["types"]:
        raise ConfigError("config_invalid", f'{where} must have a non-empty "types" object')
    out: dict[str, Any] = {"types": {}}
    for name, r in data["types"].items():
        if not isinstance(r, dict):
            raise ConfigError("config_invalid", f"{where}: types.{name} must be an object")
        try:
            keywords = [[str(k["term"]).casefold(), float(k["weight"])] for k in r.get("keywords", [])]
            patterns = []
            for p in r.get("patterns", []):
                re.compile(p["regex"])                     # fail at load, not mid-run
                patterns.append([str(p.get("name") or p["regex"]), str(p["regex"]), float(p["weight"])])
            entities = {str(k).upper(): float(v) for k, v in (r.get("entity_weights") or {}).items()}
            negatives = [str(n).casefold() for n in r.get("negative_signals", []) if str(n).strip()]
            threshold = float(r.get("classification_threshold", 0.6))
        except (KeyError, TypeError, ValueError, re.error) as exc:
            raise ConfigError("config_invalid", f"{where}: types.{name}: {exc}") from exc
        out["types"][name] = {"keywords": keywords, "patterns": patterns, "entities": entities,
                              "negatives": negatives, "threshold": threshold}
    return out


def score(rules: dict[str, Any], text: str) -> dict[str, dict[str, Any]]:
    lowered = text.casefold()
    present = {name for name, rx in _ENTITIES.items() if rx.search(text)}
    out = {}
    for name, r in rules["types"].items():
        hits: list[str] = []
        total = 0.0
        for term, w in r["keywords"]:
            if term and term in lowered:
                total += w
                hits.append(f"keyword:{term}")
        for pname, regex, w in r["patterns"]:
            if _compiled(regex).search(text):
                total += w
                hits.append(f"pattern:{pname}")
        for ent, w in r["entities"].items():
            if ent in present:
                total += w
                hits.append(f"entity:{ent}")
        negatives = [n for n in r["negatives"] if n in lowered]
        total -= NEGATIVE_PENALTY * len(negatives)
        out[name] = {"score": round(total, 4), "threshold": r["threshold"], "matched": total >= r["threshold"],
                     "hits": hits[:20], "negative_signals": negatives}
    return out


def classify(cfg: Any, text: str) -> dict[str, Any]:
    """Pick the email type for one submission. ``cfg`` is the resolved ExtractionConfig."""
    if not cfg.detection:
        return {"status": "unclassified", "type": cfg.default_type, "score": None, "scores": {}}
    scores = score(cfg.detection, text)
    ranked = sorted(((v["score"], k) for k, v in scores.items() if v["matched"]), reverse=True)
    margin = float(cfg.classification.get("ambiguity_margin", 0.1))
    if not ranked:
        best = max(scores, key=lambda k: scores[k]["score"])
        return {"status": "out_of_scope", "type": None, "nearest": best, "score": scores[best]["score"],
                "scores": scores}
    top_score, top = ranked[0]
    if not cfg.extractable(top):
        return {"status": "out_of_scope", "type": top, "score": top_score, "scores": scores}
    status = "ambiguous" if len(ranked) > 1 and top_score - ranked[1][0] < margin else "matched"
    out = {"status": status, "type": top, "score": top_score, "scores": scores}
    if status == "ambiguous":
        out["runner_up"] = ranked[1][1]
    return out


def submission_text(sub: Any, docs: list[Any], limit: int = 200_000) -> str:
    """Subject plus the text of every readable document, up to ``limit`` characters."""
    parts, size = [getattr(sub, "subject", None) or ""], 0
    for d in docs:
        for b in d.blocks:
            if size > limit:
                break
            parts.append(b.text)
            size += len(b.text) + 1
    return "\n".join(p for p in parts if p)
