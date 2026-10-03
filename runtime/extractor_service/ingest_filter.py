"""Ingestion filter: decide, before anything is parsed, which items to ignore.

Every item a submission yields (the input file, each email attachment, each
zip member, each attached email) is checked against lookup data. An ignored
item is never parsed and never reaches the model; it is recorded in
``metadata.skipped`` with the rule that matched, and it raises no flag (it
was ignored on purpose).

Lookup data lives in two places, both optional, both read in this order:

    <CONFIG_ROOT>/lookups/                     every client and use case
    <CONFIG_ROOT>/<client>/<usecase>/<version>/lookups/    this config only

and in each, two files:

``ingestion.json``: plain lists, for the common cases::

    {
      "ignore_names":  ["docusign", "*.ics", "re:^certificate of completion"],
      "ignore_hashes": ["<sha256 hex>", "md5:<md5 hex>"],
      "ignore_kinds":  ["image"]
    }

  A name entry matches the file name case-insensitively: plain text matches
  anywhere in the name ("docusign" ignores "DocuSign_Summary.pdf"), an entry
  with ``*`` / ``?`` / ``[`` is a glob over the whole name, and ``re:`` starts a
  regular expression. A hash entry is the content's SHA-256 (or ``md5:``).

``ingestion.decision.json``: a ZEN engine decision (JDM, as exported by the
  ZEN editor) for anything the lists cannot say. It is evaluated with the
  item below and should output ``{"action": "ignore" | "keep", "rule": "..."}``::

    {"name", "path", "extension", "kind", "size", "sha256", "md5",
     "container" (file | email | msg | zip | embedded), "depth",
     "sender", "sender_domain", "subject", "client", "usecase"}

The first rule that says "ignore" wins. A decision that fails to evaluate
keeps the item and adds the ``ingestion_rule_error`` flag, so a broken rule
never silently drops a document.
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import ConfigError

LISTS_FILE = "ingestion.json"
DECISION_FILE = "ingestion.decision.json"
_HEX = re.compile(r"^[0-9a-f]+$")


@dataclass(frozen=True)
class Verdict:
    ignore: bool
    rule: str = ""
    error: str | None = None


@dataclass
class _Rules:
    scope: str                                     # "global" or "config"
    names: list[tuple[str, Any]] = field(default_factory=list)   # (entry, matcher)
    sha256: set[str] = field(default_factory=set)
    md5: set[str] = field(default_factory=set)
    kinds: set[str] = field(default_factory=set)
    decision: Any = None

    def empty(self) -> bool:
        return not (self.names or self.sha256 or self.md5 or self.kinds or self.decision)


def _name_matcher(entry: str, where: str):
    if not isinstance(entry, str) or not entry.strip():
        raise ConfigError("config_invalid", f"{where}: ignore_names entries must be non-empty strings")
    text = entry.strip()
    if text.lower().startswith("re:"):
        try:
            rx = re.compile(text[3:], re.I)
        except re.error as exc:
            raise ConfigError("config_invalid", f"{where}: bad regular expression {text!r}: {exc}") from exc
        return lambda name: bool(rx.search(name))
    low = text.casefold()
    if any(ch in low for ch in "*?["):
        return lambda name: fnmatch.fnmatchcase(name.casefold(), low)
    return lambda name: low in name.casefold()


def _load(folder: Path, scope: str) -> _Rules:
    rules = _Rules(scope)
    lists = folder / LISTS_FILE
    where = f"{scope} lookups/{LISTS_FILE}"
    if lists.is_file():
        try:
            data = json.loads(lists.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ConfigError("config_invalid", f"{where} is not valid JSON: {exc}") from exc
        if not isinstance(data, dict) or set(data) - {"ignore_names", "ignore_hashes", "ignore_kinds",
                                                       "description"}:
            raise ConfigError("config_invalid", f"{where}: keys are ignore_names, ignore_hashes, ignore_kinds")
        rules.names = [(e, _name_matcher(e, where)) for e in data.get("ignore_names", [])]
        for h in data.get("ignore_hashes", []):
            algo, _, value = str(h).strip().lower().rpartition(":")
            algo = algo or ("md5" if len(value) == 32 else "sha256")
            if algo not in ("sha256", "md5") or not _HEX.match(value) or \
                    len(value) != (64 if algo == "sha256" else 32):
                raise ConfigError("config_invalid", f"{where}: {h!r} is not a sha256 or md5:<hex> hash")
            (rules.sha256 if algo == "sha256" else rules.md5).add(value)
        rules.kinds = {str(k).lower() for k in data.get("ignore_kinds", [])}
    decision = folder / DECISION_FILE
    if decision.is_file():
        import zen
        try:
            content = decision.read_text(encoding="utf-8")
            json.loads(content)
            rules.decision = zen.ZenEngine().create_decision(content)
        except Exception as exc:
            raise ConfigError("config_invalid", f"{scope} lookups/{DECISION_FILE}: {exc}") from exc
    return rules


class IngestionFilter:
    """Global then per-config rules; ``check(item)`` says whether to ignore it."""

    def __init__(self, layers: list[_Rules]) -> None:
        self.layers = [r for r in layers if not r.empty()]

    @classmethod
    def load(cls, config_root: Path, config_folder: Path | None) -> "IngestionFilter":
        layers = [_load(Path(config_root) / "lookups", "global")]
        if config_folder is not None:
            layers.append(_load(Path(config_folder) / "lookups", "config"))
        return cls(layers)

    @property
    def active(self) -> bool:
        return bool(self.layers)

    def check(self, item: dict[str, Any]) -> Verdict:
        if not self.layers:
            return Verdict(False)
        name = str(item.get("name", ""))
        for r in self.layers:
            for entry, match in r.names:
                if match(name):
                    return Verdict(True, f"{r.scope}:name:{entry}")
            if item.get("sha256") in r.sha256:
                return Verdict(True, f"{r.scope}:sha256:{item['sha256'][:12]}")
            if r.md5 and item.get("md5") in r.md5:
                return Verdict(True, f"{r.scope}:md5:{item['md5'][:12]}")
            if str(item.get("kind", "")).lower() in r.kinds:
                return Verdict(True, f"{r.scope}:kind:{item['kind']}")
            if r.decision is not None:
                try:
                    out = (r.decision.evaluate(item) or {}).get("result") or {}
                except Exception as exc:                      # a broken rule keeps the item
                    return Verdict(False, error=f"{r.scope}:decision:{exc}"[:200])
                if str(out.get("action", "")).lower() == "ignore":
                    return Verdict(True, f"{r.scope}:decision:{out.get('rule') or 'ignore'}")
        return Verdict(False)


def item_facts(name: str, path: str, data: bytes, kind: str, container: str, depth: int,
               sender: str | None, subject: str | None, client: str, usecase: str) -> dict[str, Any]:
    """The facts a rule can test about one item."""
    base = name.rsplit("/", 1)[-1]
    domain = ""
    if sender:
        m = re.search(r"@([A-Za-z0-9.-]+)", sender)
        domain = m.group(1).lower() if m else ""
    return {"name": base, "path": path, "extension": base.rsplit(".", 1)[-1].lower() if "." in base else "",
            "kind": kind, "size": len(data), "sha256": hashlib.sha256(data).hexdigest(),
            "md5": hashlib.md5(data, usedforsecurity=False).hexdigest(), "container": container, "depth": depth,
            "sender": sender or "", "sender_domain": domain, "subject": subject or "",
            "client": client, "usecase": usecase}
