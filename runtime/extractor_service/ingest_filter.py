"""Ingestion filter: decide, before anything is parsed, which items to ignore.

Every item a submission yields (the input file, each email attachment, each
zip member, each attached email) is checked against lookup data. An ignored
item is never parsed and never reaches the model; it is recorded in
``metadata.skipped`` with the rule that matched, and it raises no flag (it
was ignored on purpose).

Lookup data lives outside the version folders, at three levels, so it can
change without cutting a config version:

    <CONFIG_ROOT>/lookups/                     every client and use case (global)
    <CONFIG_ROOT>/<client>/lookups/            every use case of one client
    <CONFIG_ROOT>/<client>/<usecase>/lookups/  one client's use case

The most specific level is consulted first: use case, then client, then
global. Within a level, a "keep" match wins over an "ignore" match, and the
first level with a verdict decides; so a client can keep a document type the
global list ignores (``keep_names`` / ``keep_hashes``, or a decision returning
``"keep"``).

and in each, two files:

``ingestion.json``: plain lists, for the common cases::

    {
      "ignore_names":  ["docusign", "*.ics", "re:^certificate of completion"],
      "ignore_hashes": ["<sha256 hex>", "md5:<md5 hex>"],
      "ignore_kinds":  ["image"],
      "keep_names":    ["docusign invoice"],      (overrides wider levels)
      "keep_hashes":   []
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

A decision that fails to evaluate
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


LEVELS = ("usecase", "client", "global")          # most specific first
_KEYS = {"ignore_names", "ignore_hashes", "ignore_kinds", "keep_names", "keep_hashes", "description"}


@dataclass
class _Rules:
    scope: str                                     # "usecase", "client" or "global"
    names: list[tuple[str, Any]] = field(default_factory=list)   # (entry, matcher)
    sha256: set[str] = field(default_factory=set)
    md5: set[str] = field(default_factory=set)
    kinds: set[str] = field(default_factory=set)
    keep_names: list[tuple[str, Any]] = field(default_factory=list)
    keep_sha256: set[str] = field(default_factory=set)
    keep_md5: set[str] = field(default_factory=set)
    decision: Any = None
    source: str = ""

    def empty(self) -> bool:
        return not (self.names or self.sha256 or self.md5 or self.kinds or self.decision
                    or self.keep_names or self.keep_sha256 or self.keep_md5)

    def describe(self) -> dict[str, Any]:
        return {"level": self.scope, "path": self.source,
                "ignore_names": [e for e, _ in self.names], "keep_names": [e for e, _ in self.keep_names],
                "ignore_hashes": sorted(self.sha256) + [f"md5:{h}" for h in sorted(self.md5)],
                "keep_hashes": sorted(self.keep_sha256) + [f"md5:{h}" for h in sorted(self.keep_md5)],
                "ignore_kinds": sorted(self.kinds), "decision": self.decision is not None}


def _hashes(values: Any, where: str) -> tuple[set[str], set[str]]:
    sha, md5 = set(), set()
    for h in values or []:
        algo, _, value = str(h).strip().lower().rpartition(":")
        algo = algo or ("md5" if len(value) == 32 else "sha256")
        if algo not in ("sha256", "md5") or not _HEX.match(value) or \
                len(value) != (64 if algo == "sha256" else 32):
            raise ConfigError("config_invalid", f"{where}: {h!r} is not a sha256 or md5:<hex> hash")
        (sha if algo == "sha256" else md5).add(value)
    return sha, md5


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
    rules = _Rules(scope, source=str(folder))
    lists = folder / LISTS_FILE
    where = f"{scope} lookups/{LISTS_FILE}"
    if lists.is_file():
        try:
            data = json.loads(lists.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ConfigError("config_invalid", f"{where} is not valid JSON: {exc}") from exc
        if not isinstance(data, dict) or set(data) - _KEYS:
            raise ConfigError("config_invalid", f"{where}: keys are {', '.join(sorted(_KEYS - {'description'}))}")
        rules.names = [(e, _name_matcher(e, where)) for e in data.get("ignore_names", [])]
        rules.keep_names = [(e, _name_matcher(e, where)) for e in data.get("keep_names", [])]
        rules.sha256, rules.md5 = _hashes(data.get("ignore_hashes"), where)
        rules.keep_sha256, rules.keep_md5 = _hashes(data.get("keep_hashes"), where)
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


def folders(config_root: Path, client: str | None, usecase: str | None) -> list[tuple[str, Path]]:
    """The lookup folders for a client and use case, most specific first."""
    root = Path(config_root)
    out = []
    if client and usecase:
        out.append(("usecase", root / client / usecase / "lookups"))
    if client:
        out.append(("client", root / client / "lookups"))
    out.append(("global", root / "lookups"))
    return out


def fingerprint(config_root: Path, client: str | None, usecase: str | None) -> str:
    """A hash over every lookup file that applies, so a change is picked up without a new version."""
    h = hashlib.sha256()
    for level, folder in folders(config_root, client, usecase):
        if folder.is_dir():
            for p in sorted(x for x in folder.rglob("*") if x.is_file()):
                h.update(f"{level}/{p.relative_to(folder).as_posix()}".encode() + b"\0" + p.read_bytes() + b"\0")
    return h.hexdigest()


class IngestionFilter:
    """Use case, client, then global rules; ``check(item)`` says whether to ignore it."""

    def __init__(self, layers: list[_Rules]) -> None:
        self.layers = [r for r in layers if not r.empty()]

    @classmethod
    def load(cls, config_root: Path, client: str | None = None, usecase: str | None = None) -> "IngestionFilter":
        return cls([_load(folder, level) for level, folder in folders(config_root, client, usecase)])

    @property
    def active(self) -> bool:
        return bool(self.layers)

    def describe(self) -> list[dict[str, Any]]:
        return [r.describe() for r in self.layers]

    def check(self, item: dict[str, Any]) -> Verdict:
        if not self.layers:
            return Verdict(False)
        name = str(item.get("name", ""))
        sha, md5 = item.get("sha256"), item.get("md5")
        for r in self.layers:
            for entry, match in r.keep_names:
                if match(name):
                    return Verdict(False, f"{r.scope}:keep_name:{entry}")
            if sha in r.keep_sha256:
                return Verdict(False, f"{r.scope}:keep_sha256:{sha[:12]}")
            if r.keep_md5 and md5 in r.keep_md5:
                return Verdict(False, f"{r.scope}:keep_md5:{md5[:12]}")
            for entry, match in r.names:
                if match(name):
                    return Verdict(True, f"{r.scope}:name:{entry}")
            if sha in r.sha256:
                return Verdict(True, f"{r.scope}:sha256:{sha[:12]}")
            if r.md5 and md5 in r.md5:
                return Verdict(True, f"{r.scope}:md5:{md5[:12]}")
            if str(item.get("kind", "")).lower() in r.kinds:
                return Verdict(True, f"{r.scope}:kind:{item['kind']}")
            if r.decision is not None:
                try:
                    out = (r.decision.evaluate(item) or {}).get("result") or {}
                except Exception as exc:                      # a broken rule keeps the item
                    return Verdict(False, error=f"{r.scope}:decision:{exc}"[:200])
                action = str(out.get("action", "")).lower()
                if action == "ignore":
                    return Verdict(True, f"{r.scope}:decision:{out.get('rule') or 'ignore'}")
                if action == "keep":
                    return Verdict(False, f"{r.scope}:decision:{out.get('rule') or 'keep'}")
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
