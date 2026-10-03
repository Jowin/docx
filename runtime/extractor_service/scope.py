"""Which skills apply to a document: pattern fingerprints.

A skill's front matter may say which documents it is for::

    applies_to:
      sender_domains: [hooli.example]           # the email's sender, or a subdomain of it
      file_names: ["hooli-remit*.csv"]          # any item's file name (glob, case-insensitive)
      headers: [Our Ref, Bill Date, Biller]     # table headers seen in the documents...
      min_header_share: 0.6                     # ...at least this share of them (default 0.6)
      texts: ["remittance advice"]              # any of these phrases anywhere in the documents

Criteria are alternatives: one matching is enough. A skill without
``applies_to`` applies to every document, as before. Design-time writes a
fingerprint for every learned pattern, so one pattern's labels and anchors
cannot misread another pattern's documents.
"""
from __future__ import annotations

import fnmatch
import re
from typing import Any

from .errors import ConfigError

KEYS = {"sender_domains", "file_names", "headers", "min_header_share", "texts"}
MAX_TEXT = 200_000


def validate(name: str, spec: Any) -> dict[str, Any]:
    if spec is None:
        return {}
    if not isinstance(spec, dict) or set(spec) - KEYS:
        raise ConfigError("config_invalid", f"skill {name!r}: applies_to keys are {sorted(KEYS)}")
    for k in KEYS - {"min_header_share"}:
        v = spec.get(k, [])
        if not isinstance(v, list) or not all(isinstance(x, str) and x.strip() for x in v):
            raise ConfigError("config_invalid", f"skill {name!r}: applies_to.{k} must be a list of strings")
    share = spec.get("min_header_share", 0.6)
    if not isinstance(share, (int, float)) or not 0 < share <= 1:
        raise ConfigError("config_invalid", f"skill {name!r}: applies_to.min_header_share must be in (0, 1]")
    return spec


def facts(sub: Any, docs: list[Any]) -> dict[str, Any]:
    """What a fingerprint is tested against, from the submission and its documents."""
    domain = ""
    if getattr(sub, "sender", None):
        m = re.search(r"@([A-Za-z0-9.-]+)", sub.sender)
        domain = m.group(1).lower() if m else ""
    names = [sub.name] + [i.name.rsplit("/", 1)[-1] for i in getattr(sub, "items", [])]
    headers = {" ".join(b.text.split()).casefold() for d in docs for t in d.tables for b in t.header if b.text}
    parts, size = [], 0
    for d in docs:
        for b in d.blocks:
            if size > MAX_TEXT:
                break
            parts.append(b.text)
            size += len(b.text) + 1
    if getattr(sub, "subject", None):
        parts.append(sub.subject)
    return {"sender_domain": domain, "file_names": [n.casefold() for n in names if n],
            "headers": headers, "text": " ".join(" ".join(parts).split()).casefold()}


def applies(spec: dict[str, Any], f: dict[str, Any]) -> bool:
    if not spec:
        return True
    dom = f.get("sender_domain", "")
    for d in spec.get("sender_domains", []):
        d = d.casefold().lstrip("@")
        if dom and (dom == d or dom.endswith("." + d)):
            return True
    for g in spec.get("file_names", []):
        if any(fnmatch.fnmatchcase(n, g.casefold()) for n in f.get("file_names", [])):
            return True
    wanted = [" ".join(h.split()).casefold() for h in spec.get("headers", [])]
    if wanted:
        have = f.get("headers", set())
        if sum(h in have for h in wanted) / len(wanted) >= float(spec.get("min_header_share", 0.6)):
            return True
    text = f.get("text", "")
    return any(" ".join(t.split()).casefold() in text for t in spec.get("texts", []))
