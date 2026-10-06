"""Rules tool: deterministic transformation and lookup with the ZEN engine.

A rule set is a folder of ZEN decisions (JDM, as the GoRules ZEN editor exports
them): one *entry* decision, and any number of lookup decisions it calls with
a decision node, each named by its path in the folder::

    transform.decision.json      the entry: normalise, derive, look up, flag
    tables/portfolios.json       a decision table: account -> portfolio
    tables/purpose_codes.json    a decision table: transaction type -> cash purpose code

The tool is pure: the same rules and input give the same output, with no model
call and no state. To keep it so, only these node types are accepted:

    inputNode, outputNode, decisionTableNode, expressionNode, switchNode, decisionNode

(no function nodes, which run JavaScript, and no custom nodes), and an
expression may not read the clock (``date('now')``, ``now()``). Anything else
is refused when the rule set is loaded, not when a record reaches it.

Many inputs are evaluated in one call (``evaluate_many``), through the engine's
batch API: about 12 microseconds per input for a small decision.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from .common import ToolError

ALLOWED_NODES = frozenset({"inputNode", "outputNode", "decisionTableNode", "expressionNode", "switchNode",
                           "decisionNode"})
_CLOCK = re.compile(r"""\bnow\s*\(|\(\s*['"]now['"]\s*\)""", re.I)
MAX_FILES = 200
MAX_FILE_BYTES = 5 * 1024 * 1024


def check_decision(content: Any, name: str = "decision") -> dict[str, Any]:
    """Parse and vet one JDM decision; returns it as a dict, or raises ToolError(rules_invalid)."""
    if isinstance(content, (bytes, str)):
        try:
            content = json.loads(content)
        except json.JSONDecodeError as exc:
            raise ToolError("rules_invalid", f"{name}: not valid JSON: {exc}") from exc
    if not isinstance(content, dict) or not isinstance(content.get("nodes"), list) \
            or not isinstance(content.get("edges"), list):
        raise ToolError("rules_invalid", f"{name}: a ZEN decision has 'nodes' and 'edges' lists")
    kinds = [n.get("type") for n in content["nodes"] if isinstance(n, dict)]
    bad = sorted({str(k) for k in kinds if k not in ALLOWED_NODES})
    if bad:
        raise ToolError("rules_invalid", f"{name}: node type(s) {', '.join(bad)} are not allowed "
                                         f"(allowed: {', '.join(sorted(ALLOWED_NODES))})")
    if "inputNode" not in kinds or "outputNode" not in kinds:
        raise ToolError("rules_invalid", f"{name}: needs an input node and an output node")
    if _CLOCK.search(json.dumps(content)):
        raise ToolError("rules_invalid", f"{name}: expressions may not read the clock (now)")
    return content


def _plain(value: Any) -> Any:
    """JSON-ready: a Decimal becomes a number when a float holds it exactly, else its text."""
    from decimal import Decimal
    if isinstance(value, Decimal):
        f = float(value)
        return f if Decimal(repr(f)) == value else str(value)
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def dumps(context: Any) -> str:
    """The engine takes JSON text: numbers keep their digits (it computes in decimal, not float)."""
    return json.dumps(_plain(context), ensure_ascii=False)


class RuleSet:
    """A vetted folder (or dict) of decisions with one ZEN engine over them."""

    def __init__(self, decisions: dict[str, dict[str, Any]], entry: str, *, source: str = "") -> None:
        import zen
        if entry not in decisions:
            raise ToolError("rules_invalid", f"{source or 'rules'}: no entry decision {entry!r}")
        for key, content in decisions.items():
            for node in content["nodes"]:
                if node.get("type") == "decisionNode":
                    ref = str((node.get("content") or {}).get("key") or "")
                    if ref not in decisions:
                        raise ToolError("rules_invalid", f"{key}: decision node {node.get('name')!r} calls "
                                                         f"{ref!r}, which is not in the rule set")
        self.decisions = decisions
        self.entry = entry
        self.source = source
        self.sha256 = hashlib.sha256(json.dumps(decisions, sort_keys=True).encode()).hexdigest()
        self._engine = zen.ZenEngine({"loader": {"type": "static", "content": decisions}})
        try:
            self._engine.get_decision(entry).validate()
        except Exception as exc:                               # noqa: BLE001 - the engine's message
            raise ToolError("rules_invalid", f"{source or entry}: {exc}") from exc

    @classmethod
    def from_folder(cls, folder: Path, entry: str) -> "RuleSet":
        """``entry`` and every ``tables/**/*.json`` under ``folder``."""
        folder = Path(folder)
        files = [folder / entry] + sorted((folder / "tables").rglob("*.json")) if (folder / "tables").is_dir() \
            else [folder / entry]
        if len(files) > MAX_FILES:
            raise ToolError("rules_invalid", f"{folder}: more than {MAX_FILES} decisions")
        decisions = {}
        for p in files:
            if p.stat().st_size > MAX_FILE_BYTES:
                raise ToolError("rules_invalid", f"{p.name}: larger than {MAX_FILE_BYTES // 1024 // 1024} MB")
            key = p.relative_to(folder).as_posix()
            decisions[key] = check_decision(p.read_bytes(), key)
        return cls(decisions, entry, source=str(folder))

    def evaluate(self, context: dict[str, Any], *, trace: bool = False) -> dict[str, Any]:
        """One input -> {"result": ..., "trace"?: ...}; raises ToolError(rules_failed)."""
        try:
            out = self._engine.evaluate(self.entry, dumps(context), {"trace": trace} if trace else None)
        except Exception as exc:                               # noqa: BLE001
            raise ToolError("rules_failed", str(exc)[:500]) from exc
        res = {"result": out.get("result") or {}}
        if trace:
            res["trace"] = {v.get("name") or k: {"input": v.get("input"), "output": v.get("output"),
                                                 "matched": v.get("traceData")}
                            for k, v in sorted((out.get("trace") or {}).items(), key=lambda kv: kv[1].get("order", 0))}
        return res

    def evaluate_many(self, contexts: list[dict[str, Any]]) -> list[tuple[dict[str, Any] | None, str | None]]:
        """Each input -> (result, None) or (None, error), in order."""
        if not contexts:
            return []
        out = self._engine.evaluate_batch([{"key": self.entry, "context": dumps(c)} for c in contexts])
        res: list[tuple[dict[str, Any] | None, str | None]] = []
        for r in out:
            if r.get("success"):
                res.append(((r.get("data") or {}).get("result") or {}, None))
            else:
                err = r.get("error")
                res.append((None, (json.dumps(err, default=str) if not isinstance(err, str) else err)[:300]))
        return res


def evaluate(decision: Any, context: dict[str, Any] | None = None, *, contexts: list[dict[str, Any]] | None = None,
             tables: dict[str, Any] | None = None, trace: bool = False) -> dict[str, Any]:
    """The tool call: one decision (plus the lookup tables it calls) over one input or many.

    Returns {"tool", "result"} for one input (with "trace" when asked), or
    {"tool", "results": [{"result"} | {"error"}]} for many.
    """
    decisions = {"main.json": check_decision(decision, "decision")}
    for key, content in (tables or {}).items():
        decisions[str(key)] = check_decision(content, str(key))
    rules = RuleSet(decisions, "main.json")
    if contexts is not None:
        return {"tool": "rules", "results": [{"result": r} if e is None else {"error": e}
                                             for r, e in rules.evaluate_many(contexts)]}
    return {"tool": "rules", **rules.evaluate(context or {}, trace=trace)}
