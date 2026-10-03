"""Pattern Skill Writer: turn one failed extraction into a learned skill.

A learned skill is a markdown file in the runtime config's ``skills/`` folder
with a YAML front matter of machine-readable hints, which the runtime folds
into its data dictionary (labels become aliases; anchors read values that sit
mid-sentence), and a body the model reads as part of its system prompt:

    ---
    name: hooli-remittance
    kind: learned-pattern
    pattern: hooli-remittance
    object: invoice
    generated_by: design-agent:pattern-skill-writer@0.1.0
    written_by: stub@0.1.0
    hints:
      fields:
        invoice_number: {labels: [Our Ref]}
        total_amount: {anchors: [kindly remit]}
    ---
    ## Skill: hooli-remittance pattern
    ...

Where the hints come from, in order:

1. **Ground truth, located in the evidence** (mechanical). For each failing
   field the expected value is found in the runtime's evidence blocks; the text
   before it in the same cell or line is a label ("Our Ref: RA-501"), a phrase
   before it mid-sentence (or any text before it inside a table's body) is an
   anchor ("... kindly remit $310.50"), and a
   value alone in a cell takes the label to its left or its column header.
   Line-item columns are labelled from the header above the expected values.
2. **Reference text** (mechanical). Lines like ``invoice_number: "Our Ref"``
   and sentences that quote a label next to a field's name.
3. **No ground truth** (heuristic). ``Label: value`` blocks and table headers
   no field claims, whose words overlap a failing field's name, aliases or
   description and whose value parses as that field's type.
4. **Judgment** through the ModelClient seam (task ``pattern_skill.write``):
   the stub writes a deterministic body from the hints; the gateway client
   asks a model, which may also propose hints. Model hints are validated
   against the dictionary like any other.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

import yaml

from ..records import Record
from ..model.base import ModelRequest
from .base import AgentError, DesignAgent
from .extraction_judge import Failure

MAX_HINTS_PER_FIELD = 8
MAX_HINT_CHARS = 80
MAX_LABEL_WORDS = 4
ANCHOR_WORDS = 3
_SEPARATORS = " \t:#=-–|"
_STOP = {"the", "a", "an", "of", "for", "to", "and", "or", "in", "on", "by", "is", "be", "this",
         "that", "with", "from", "your", "our", "no", "number", "date", "value", "amount"}
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August",
           "September", "October", "November", "December"]


@dataclass(kw_only=True)
class Derivation(Record):
    field: str
    kind: str                   # label | anchor
    hint: str
    #: How it was found: ground_truth | reference_text | heuristic | model
    basis: str
    #: The evidence it was read from, as doc_id#locator, when there is one.
    at: str | None = None


@dataclass(kw_only=True)
class PatternSkillWriterInput(Record):
    pattern_name: str
    #: The data object the pattern produces (the dictionary's name).
    object: str | None = None
    dictionary: dict[str, Any]
    failures: list[Failure] = field(default_factory=list)
    failing_fields: list[str] = field(default_factory=list)
    ground_truth: list[dict[str, Any]] | None = None
    #: The runtime's evidence documents (``extractor_service.cli`` evidence).
    evidence: list[dict[str, Any]] = field(default_factory=list)
    reference_text: str | None = None
    #: Hints this pattern's skill already carries; the new skill keeps them.
    existing_hints: dict[str, Any] = field(default_factory=dict)
    attempt: int = 1
    #: Joins a model call to its learning run in gateway logs (client, usecase, ...).
    trace: dict[str, str] = field(default_factory=dict)
    #: Hints learning memory remembers as harmful: [{"field", "kind", "hint"}]. Never proposed.
    avoid: list[dict[str, Any]] = field(default_factory=list)
    #: "pattern": the skill applies only to documents matching its fingerprint;
    #: "global": to every document of the config version.
    scope: Literal["pattern", "global"] = "pattern"
    #: The pattern skill's existing fingerprint, kept and extended.
    existing_applies_to: dict[str, Any] = field(default_factory=dict)
    #: The sample's input metadata from the runtime (name, sender, subject).
    input: dict[str, Any] = field(default_factory=dict)


@dataclass(kw_only=True)
class PatternSkillWriterOutput(Record):
    name: str
    hints: dict[str, Any]
    #: Hints this call added on top of ``existing_hints``.
    new_hints: list[Derivation] = field(default_factory=list)
    body: str
    #: The whole skill file: front matter plus body.
    markdown: str
    written_by: str
    #: The fingerprint that scopes the skill (empty = applies to every document).
    applies_to: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


# ------------------------------------------------------------------ hint sets


class _Hints:
    """fields -> {labels, anchors, items -> sub -> {labels}} with validation and order."""

    def __init__(self, spec: dict[str, dict[str, Any]], existing: dict[str, Any] | None = None,
                 avoid: list[dict[str, Any]] | None = None) -> None:
        self.spec = spec
        self.data: dict[str, dict[str, Any]] = {}
        self.added: list[Derivation] = []
        self.avoided: list[str] = []
        self.avoid = {(a["field"], "labels" if a["kind"] in ("label", "labels") else "anchors",
                       " ".join(str(a["hint"]).split()).casefold()) for a in (avoid or [])}
        self.known = {n: {a.casefold() for a in f.get("aliases", [])} | {n.replace("_", " ")}
                      for n, f in spec.items()}
        for name, h in ((existing or {}).get("fields") or {}).items():
            for kind in ("labels", "anchors"):
                for v in h.get(kind, []) or []:
                    self.add(name, kind, v, None, record=False)
            for sub, sh in (h.get("items") or {}).items():
                for v in sh.get("labels", []) or []:
                    self.add(f"{name}.{sub}", "labels", v, None, record=False)

    def _slot(self, path: str, kind: str) -> list[str] | None:
        name, _, sub = path.partition(".")
        f = self.spec.get(name)
        if f is None:
            return None
        entry = self.data.setdefault(name, {})
        if sub:
            if f["type"] != "array" or sub not in {s["name"] for s in f.get("items", [])} or kind != "labels":
                return None
            return entry.setdefault("items", {}).setdefault(sub, {}).setdefault("labels", [])
        if f["type"] == "array":
            return None
        return entry.setdefault(kind, [])

    def add(self, path: str, kind: str, value: str, d: Derivation | None, *, record: bool = True) -> bool:
        value = " ".join(str(value).split()).strip(_SEPARATORS + "\"'")
        if not value or len(value) > MAX_HINT_CHARS or not re.search(r"[A-Za-z]", value):
            return False
        name, _, sub = path.partition(".")
        if record and (path, kind, value.casefold()) in self.avoid:
            self.avoided.append(f"{path}:{kind[:-1]}:{value}")         # memory says it broke another sample
            return False
        if kind == "labels" and not sub:
            # a label another field already answers to would steal that field's values
            if any(value.casefold() in aliases for other, aliases in self.known.items() if other != name):
                return False
            if value.casefold() in self.known.get(name, set()):
                return False
        slot = self._slot(path, kind)
        if slot is None or len(slot) >= MAX_HINTS_PER_FIELD:
            return False
        if value.casefold() in {v.casefold() for v in slot}:
            return False
        slot.append(value)
        if record and d is not None:
            self.added.append(d)
        return True

    def as_dict(self) -> dict[str, Any]:
        fields: dict[str, Any] = {}
        for name in self.spec:               # dictionary order, empty entries dropped
            h = self.data.get(name) or {}
            entry: dict[str, Any] = {k: h[k] for k in ("labels", "anchors") if h.get(k)}
            items = {s: v for s, v in (h.get("items") or {}).items() if v.get("labels")}
            if items:
                entry["items"] = items
            if entry:
                fields[name] = entry
        return {"fields": fields} if fields else {}


# ------------------------------------------------------------------ value location


def _num(text: str) -> Decimal | None:
    try:
        return Decimal(text.replace(",", "").replace("'", ""))
    except InvalidOperation:
        return None


def value_spans(ftype: str, value: Any, text: str) -> list[tuple[int, int]]:
    """Where ``value`` is written in ``text``, as (start, end) spans."""
    if value is None or value == "":
        return []
    spans: list[tuple[int, int]] = []
    if ftype in ("decimal", "integer"):
        try:
            want = Decimal(str(value).replace(",", ""))
        except InvalidOperation:
            return []
        for m in re.finditer(r"(?<![\w.])-?\d[\d,']*(?:\.\d+)?", text):
            got = _num(m.group(0))
            if got is not None and got == want:
                # take a currency symbol or code just before the number with it
                start = m.start()
                pre = re.search(r"(?:[A-Z]{3}\s?|[$€£₹¥]\s?)$", text[:start])
                spans.append((pre.start() if pre else start, m.end()))
        return spans
    forms = [str(value)]
    if ftype == "date":
        try:
            d = dt.date.fromisoformat(str(value)[:10])
        except ValueError:
            d = None
        if d:
            mon, mname = _MONTHS[d.month - 1][:3], _MONTHS[d.month - 1]
            forms = [d.isoformat(), f"{d.day:02d}/{d.month:02d}/{d.year}", f"{d.day}/{d.month}/{d.year}",
                     f"{d.month:02d}/{d.day:02d}/{d.year}", f"{d.month}/{d.day}/{d.year}",
                     f"{d.day:02d}-{d.month:02d}-{d.year}", f"{d.day:02d}.{d.month:02d}.{d.year}",
                     f"{d.day} {mon} {d.year}", f"{d.day} {mname} {d.year}", f"{d.day:02d} {mon} {d.year}",
                     f"{mon} {d.day}, {d.year}", f"{mname} {d.day}, {d.year}", f"{d.day}-{mon}-{d.year}"]
    low = text.casefold()
    for form in dict.fromkeys(forms):
        f = " ".join(form.split()).casefold()
        if not f:
            continue
        for m in re.finditer(re.escape(f), low):
            s, e = m.start(), m.end()
            # whole tokens only: "RA-5" is not inside "RA-501"
            if (s and low[s - 1].isalnum()) or (e < len(low) and low[e].isalnum()):
                continue
            spans.append((s, e))
    return sorted(set(spans))


def _clean_label(prefix: str) -> str:
    label = prefix.strip()
    label = re.sub(r"(?i)[\s:#=\-–|]+$", "", label)
    return label.strip(_SEPARATORS)


def _label_like(text: str) -> bool:
    """Short, and a name rather than a sentence: "Our Ref", not "Thank you, kindly remit"."""
    return len(text.split()) <= MAX_LABEL_WORDS and not re.search(r"[,.;!?]", text)


def _anchor_of(prefix: str) -> str:
    """The last few words before a value, after any sentence break."""
    tail = re.split(r"[,.;!?:]\s", prefix)[-1]
    return " ".join(tail.split()[-ANCHOR_WORDS:]).casefold()


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z]{2,}", text.casefold()) if w not in _STOP}


# ------------------------------------------------------------------ evidence index


class _Evidence:
    def __init__(self, docs: list[dict[str, Any]]) -> None:
        self.docs = [d for d in docs if d.get("status", "read") == "read"]
        self.cells: dict[tuple[str, str, int], list[dict]] = {}     # (doc, group, row) -> cells
        self.headers: dict[tuple[str, str, int], str] = {}          # (doc, group, col) -> header text
        self.table_rows: set[tuple[str, str]] = set()               # (doc, locator) inside a table body
        for d in self.docs:
            by_loc = {b["locator"]: b for b in d.get("blocks", [])}
            for b in d.get("blocks", []):
                if b.get("row") is not None and b.get("col") is not None:
                    self.cells.setdefault((d["doc_id"], b.get("group", ""), b["row"]), []).append(b)
            for t in d.get("tables", []):
                for loc in t.get("header", []):
                    hb = by_loc.get(loc)
                    if hb and hb.get("col") is not None and hb["text"].strip():
                        self.headers[(d["doc_id"], t.get("group", ""), hb["col"])] = hb["text"].strip()
                for row in t.get("rows", []):
                    for loc in row:
                        self.table_rows.add((d["doc_id"], loc))
        for row in self.cells.values():
            row.sort(key=lambda b: b["col"])

    def blocks(self):
        # attachments and files before an email body: the document is where the pattern lives
        for d in sorted(self.docs, key=lambda d: d.get("kind") == "email_body"):
            for b in d.get("blocks", []):
                yield d, b

    def left_label(self, d: dict, b: dict) -> str | None:
        if b.get("row") is None or b.get("col") is None:
            return None
        row = self.cells.get((d["doc_id"], b.get("group", ""), b["row"]), [])
        left = [x for x in row if x["col"] < b["col"] and x["text"].strip()]
        if left and left[-1].get("vtype", "string") == "string" and re.search(r"[A-Za-z]", left[-1]["text"]):
            return left[-1]["text"]
        return None

    def header(self, d: dict, b: dict) -> str | None:
        """The column header above a table body cell."""
        if b.get("col") is None or (d["doc_id"], b["locator"]) not in self.table_rows:
            return None
        return self.headers.get((d["doc_id"], b.get("group", ""), b["col"]))

    def near_label(self, d: dict, b: dict) -> str | None:
        """A value alone in a cell: its column header in a table, else the label cell to its left."""
        if (d["doc_id"], b["locator"]) in self.table_rows:
            return self.header(d, b)
        return self.left_label(d, b)


# ------------------------------------------------------------------ agent


class PatternSkillWriter(DesignAgent[PatternSkillWriterInput, PatternSkillWriterOutput]):
    name = "pattern-skill-writer"

    def run(self, payload: PatternSkillWriterInput) -> PatternSkillWriterOutput:
        if not _NAME_RE.match(payload.pattern_name):
            raise AgentError("pattern_name must be 1-64 letters, digits, '.', '_' or '-'",
                             code="invalid_pattern_name")
        fields = payload.dictionary.get("fields") or []
        spec = {f["name"]: f for f in fields}
        hints = _Hints(spec, payload.existing_hints, payload.avoid)
        ev = _Evidence(payload.evidence)
        notes: list[str] = []

        targets = [n for n in payload.failing_fields if n in spec]
        if not targets and payload.ground_truth:
            targets = [n for n in spec if any(n in r for r in payload.ground_truth)]
        if payload.ground_truth:
            for name in targets:
                found = self._from_ground_truth(name, spec[name], payload.ground_truth, ev, hints)
                if not found:
                    notes.append(f"no_location:{name}")
        else:
            for name in targets:
                if not self._heuristic(name, spec, ev, hints):
                    notes.append(f"no_candidate:{name}")
        if payload.reference_text:
            self._from_reference(payload.reference_text, spec, hints)

        # judgment: the body, and any hints the model sees that the rules did not
        derived = hints.as_dict()
        answer = self.model.complete(ModelRequest(
            task="pattern_skill.write",
            evidence={
                "pattern_name": payload.pattern_name,
                "object": payload.object or payload.dictionary.get("name"),
                "fields": [{k: f[k] for k in ("name", "type", "description", "aliases", "items") if k in f}
                           for f in fields],
                "failing_fields": targets,
                "failures": [f.to_dict(exclude_none=True) for f in payload.failures[:40]],
                "hints": derived,
                "derivations": [d.to_dict(exclude_none=True) for d in hints.added],
                "reference_text": payload.reference_text or "",
                "evidence_lines": self._lines(ev, targets, payload.ground_truth, spec),
                "attempt": payload.attempt,
                "trace": payload.trace,
            },
        ))
        model_hints = answer.output.get("hints") or {}
        for name, h in (model_hints.get("fields") or {}).items() if isinstance(model_hints, dict) else []:
            if not isinstance(h, dict):
                continue
            for kind in ("labels", "anchors"):
                for v in h.get(kind) or []:
                    if isinstance(v, str):
                        hints.add(name, kind, v, Derivation(field=name, kind=kind[:-1], hint=v, basis="model"))
            for sub, sh in (h.get("items") or {}).items() if isinstance(h.get("items"), dict) else []:
                for v in (sh or {}).get("labels") or []:
                    if isinstance(v, str):
                        hints.add(f"{name}.{sub}", "labels", v,
                                  Derivation(field=f"{name}.{sub}", kind="label", hint=v, basis="model"))
        body = str(answer.output.get("body") or "").strip()
        if not body:
            raise AgentError("the skill writer returned no body", code="skill_body_empty")

        final = hints.as_dict()
        notes += [f"avoided:{a}" for a in hints.avoided]
        applies_to = {} if payload.scope == "global" else self._fingerprint(payload, ev, final)
        if payload.scope == "pattern" and not applies_to:
            notes.append("no_fingerprint")
        front = {"name": payload.pattern_name, "kind": "learned-pattern", "pattern": payload.pattern_name,
                 "object": payload.object or payload.dictionary.get("name"),
                 "generated_by": self.identity, "written_by": answer.produced_by,
                 **({"applies_to": applies_to} if applies_to else {}), "hints": final}
        markdown = "---\n" + yaml.safe_dump(front, sort_keys=False, allow_unicode=True, width=1000) + \
            "---\n" + body + "\n"
        return PatternSkillWriterOutput(name=payload.pattern_name, hints=final, new_hints=hints.added,
                                        body=body, markdown=markdown, written_by=answer.produced_by,
                                        applies_to=applies_to, notes=notes)

    # -------------------------------------------------------------- fingerprint

    @staticmethod
    def _fingerprint(payload: PatternSkillWriterInput, ev: _Evidence, hints: dict[str, Any]) -> dict[str, Any]:
        """What documents of this pattern look like, so its hints stay with them (runtime scope.py).

        The sender's domain when the sample is an email, the table headers it
        carries, and, failing both, a short wordless title line. An existing
        fingerprint is kept: its headers stay, domains and texts are added to.
        """
        old = payload.existing_applies_to or {}
        out: dict[str, Any] = {}
        sender = str(payload.input.get("sender") or "")
        m = re.search(r"@([A-Za-z0-9.-]+)", sender)
        domains = list(dict.fromkeys((old.get("sender_domains") or []) + ([m.group(1).lower()] if m else [])))
        if domains:
            out["sender_domains"] = domains
        headers = list(old.get("headers") or [])
        if not headers:
            for d in ev.docs:
                by_loc = {b["locator"]: b["text"] for b in d.get("blocks", [])}
                for t in d.get("tables", []):
                    for loc in t.get("header", []):
                        text = " ".join(str(by_loc.get(loc, "")).split())
                        if text and text not in headers and re.search(r"[A-Za-z]", text):
                            headers.append(text)
            headers = headers[:12]
        if len(headers) >= 2:
            out["headers"] = headers
            out["min_header_share"] = float(old.get("min_header_share", 0.6))
        texts = list(old.get("texts") or [])
        if not out and not texts:
            labels = {h.casefold() for f in (hints.get("fields") or {}).values() for h in f.get("labels", [])}
            for _, b in ev.blocks():
                t = " ".join(b["text"].split())
                if 2 <= len(t.split()) <= 8 and len(t) <= 60 and not re.search(r"\d", t) \
                        and ":" not in t and t.casefold() not in labels:
                    texts = [t]
                    break
        if texts:
            out["texts"] = texts
        return out

    # -------------------------------------------------------------- ground truth

    def _from_ground_truth(self, name: str, f: dict, gt: list[dict], ev: _Evidence, hints: _Hints) -> bool:
        if f["type"] == "array":
            return self._items_from_ground_truth(name, f, gt, ev, hints)
        found = False
        for value in dict.fromkeys(str(r[name]) for r in gt if r.get(name) not in (None, "")):
            for d, b in ev.blocks():
                spans = value_spans(f["type"], value, b["text"])
                if not spans:
                    continue
                start, _ = spans[0]
                prefix = b["text"][:start]
                at = f"{d['doc_id']}#{b['locator']}"
                label = _clean_label(prefix)
                in_table = (d["doc_id"], b["locator"]) in ev.table_rows
                # the runtime reads "Label: value" from free cells and lines; inside a
                # table body only an anchor is read, so text there becomes an anchor
                if label and _label_like(label) and not in_table:
                    ok = hints.add(name, "labels", label, Derivation(field=name, kind="label", hint=label,
                                                                     basis="ground_truth", at=at))
                elif label:
                    anchor = _anchor_of(label)
                    ok = hints.add(name, "anchors", anchor, Derivation(field=name, kind="anchor", hint=anchor,
                                                                       basis="ground_truth", at=at))
                else:
                    near = ev.near_label(d, b)
                    ok = bool(near) and hints.add(name, "labels", _clean_label(near), Derivation(
                        field=name, kind="label", hint=_clean_label(near), basis="ground_truth", at=at))
                if ok:
                    found = True
                    break
                if label or ev.near_label(d, b):
                    found = True            # located, already known: the label is not the problem
                    break
        return found

    def _items_from_ground_truth(self, name: str, f: dict, gt: list[dict], ev: _Evidence,
                                 hints: _Hints) -> bool:
        subs = {s["name"]: s for s in f.get("items", [])}
        items = [i for r in gt for i in (r.get(name) or []) if isinstance(i, dict)]
        found = False
        for sub, sf in subs.items():
            for item in items:
                if item.get(sub) in (None, ""):
                    continue
                hit = next(((d, b) for d, b in ev.blocks() if (d["doc_id"], b["locator"]) in ev.table_rows
                            and value_spans(sf["type"], item[sub], b["text"])), None)
                if hit is None:
                    continue
                head = ev.header(*hit)
                if head:
                    hints.add(f"{name}.{sub}", "labels", head, Derivation(
                        field=f"{name}.{sub}", kind="label", hint=head, basis="ground_truth",
                        at=f"{hit[0]['doc_id']}#{hit[1]['locator']}"))
                    found = True
                break
        return found

    # -------------------------------------------------------------- no ground truth

    def _heuristic(self, name: str, spec: dict, ev: _Evidence, hints: _Hints) -> bool:
        f = spec[name]
        if f["type"] == "array":
            return False
        own = _words(" ".join([name.replace("_", " "), f.get("description", ""), *f.get("aliases", [])]))
        claimed = {a for aliases in hints.known.values() for a in aliases}
        for d, b in ev.blocks():
            text = b["text"]
            m = re.match(r"\s*([A-Za-z][^:#=]{0,60}?)\s*[:#=]\s*(\S.*)$", text)
            candidates = []
            if m:
                candidates.append((m.group(1), m.group(2)))
            head = ev.header(d, b)
            if head:
                candidates.append((head, text))
            for label, value in candidates:
                label = _clean_label(label)
                if not label or label.casefold() in claimed or not _label_like(label):
                    continue
                if not (_words(label) & own) or not self._parses(f["type"], value):
                    continue
                if hints.add(name, "labels", label, Derivation(field=name, kind="label", hint=label,
                                                               basis="heuristic",
                                                               at=f"{d['doc_id']}#{b['locator']}")):
                    return True
        return False

    @staticmethod
    def _parses(ftype: str, value: str) -> bool:
        v = value.strip()
        if ftype in ("decimal", "integer"):
            return bool(re.search(r"\d", v)) and bool(re.fullmatch(r"[^\d]{0,4}-?\d[\d,.' ]*[^\d]{0,4}", v))
        if ftype == "date":
            return bool(re.search(r"\d{1,4}[/.\-\s]\w{1,9}[/.\-\s]\d{2,4}", v))
        return bool(v)

    # -------------------------------------------------------------- reference text

    def _from_reference(self, text: str, spec: dict, hints: _Hints) -> None:
        names = {n: n for n in spec}
        names.update({n.replace("_", " "): n for n in spec})
        for line in text.splitlines():
            m = re.match(r"\s*[-*]?\s*`?([a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)?)`?\s*(?::|=|->|→)\s*(.+)$", line)
            if m and m.group(1).split(".")[0] in spec:
                path = m.group(1)
                quoted = re.findall(r"[\"“']([^\"”']{1,80})[\"”']", m.group(2))
                for label in quoted or re.split(r"\s*(?:,|;|\bor\b)\s*", m.group(2)):
                    hints.add(path, "labels", label, Derivation(field=path, kind="label", hint=label.strip(),
                                                                basis="reference_text"))
                continue
            for sentence in re.split(r"(?<=[.!?])\s+", line):
                low = sentence.casefold()
                mentioned = {n for k, n in names.items() if re.search(rf"\b{re.escape(k)}\b", low)}
                quoted = re.findall(r"[\"“]([^\"”]{1,80})[\"”]", sentence)
                if len(mentioned) == 1 and quoted:
                    (field_name,) = mentioned
                    for label in quoted:
                        hints.add(field_name, "labels", label, Derivation(
                            field=field_name, kind="label", hint=label, basis="reference_text"))

    # -------------------------------------------------------------- model context

    @staticmethod
    def _lines(ev: _Evidence, targets: list[str], gt: list[dict] | None, spec: dict) -> list[str]:
        """A small window of evidence for the model: blocks holding expected values, else the first lines."""
        out: list[str] = []
        wanted = [(spec[n]["type"], r[n]) for n in targets if spec[n]["type"] != "array"
                  for r in (gt or []) if r.get(n) not in (None, "")]
        for d, b in ev.blocks():
            if len(out) >= 60:
                break
            if not wanted or any(value_spans(t, v, b["text"]) for t, v in wanted):
                out.append(f"[{d['doc_id']}#{b['locator']}] {b['text'][:200]}")
        return out
