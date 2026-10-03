"""Deterministic stand-in for the judgment steps.

Each task is answered by a rule that is defensible in itself, so the pipeline
produces sensible artifacts end to end without a model call, and every API is
exercisable offline. Swapping in a real client changes these answers, not the
surrounding code.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

from .base import ModelRequest, ModelResponse

_STOPWORDS = {
    "the", "and", "for", "you", "your", "with", "this", "that", "from", "have",
    "has", "are", "was", "will", "can", "our", "all", "any", "please", "dear",
    "hi", "hello", "thanks", "regards", "kind", "best", "team", "find",
    "attached", "attachment", "see", "below", "above", "not", "but", "per",
}
_WORD_RE = re.compile(r"[a-z][a-z'-]{2,}")


def _seed(*parts: str) -> int:
    return int(hashlib.sha256("|".join(parts).encode()).hexdigest()[:8], 16)


class StubModelClient:
    """A ModelClient whose answers depend only on the evidence it is given."""

    name = "stub"

    def __init__(self, version: str = "0.1.0") -> None:
        self.version = version

    @property
    def _id(self) -> str:
        return f"stub@{self.version}"

    def complete(self, request: ModelRequest) -> ModelResponse:
        handler = getattr(self, f"_task_{request.task.replace('.', '_')}", None)
        if handler is None:
            raise NotImplementedError(f"stub has no answer for task {request.task!r}")
        return ModelResponse(
            task=request.task,
            output=handler(request.evidence),
            produced_by=self._id,
            deterministic=True,
        )

    # -- type discovery ----------------------------------------------------

    def _task_type_discovery_name_cluster(self, ev: dict[str, Any]) -> dict[str, Any]:
        """Name a cluster of samples. Evidence carries the labelled type when
        one exists; otherwise fall back to the most distinctive term."""
        if ev.get("labelled_type"):
            return {"name": ev["labelled_type"], "rationale": "taken from corpus labels"}
        terms = ev.get("frequent_terms") or []
        name = terms[0] if terms else "unclassified"
        return {"name": name, "rationale": "most frequent distinctive term in the cluster"}

    # -- field schema ------------------------------------------------------

    def _task_field_schema_propose_aliases(self, ev: dict[str, Any]) -> dict[str, Any]:
        """Group observed labels under canonical fields.

        Each label is assigned to at most one field: the one whose own tokens
        the label covers most completely. Assigning to every field that merely
        shares a token is what makes "Amount Due" look like an alias of
        ``due_date`` as well as ``amount``. Labels only, never values
        (CTR-28, DT-09).
        """
        fields: list[str] = ev.get("fields", [])
        observed: list[str] = ev.get("observed_labels", [])
        out: dict[str, list[str]] = {f: [] for f in fields}

        def tokens(text: str) -> set[str]:
            return {t for t in re.split(r"[^a-z0-9]+", text.lower()) if t}

        field_tokens = {f: tokens(f) for f in fields}

        for label in observed:
            label_tokens = tokens(label)
            if not label_tokens:
                continue
            best: tuple[float, float, str] | None = None
            for field_name, ftok in field_tokens.items():
                if not ftok or label_tokens == ftok:
                    continue  # identical to the canonical name, not an alias
                shared = ftok & label_tokens
                if not shared:
                    continue
                covers_field = len(shared) / len(ftok)
                covers_label = len(shared) / len(label_tokens)
                candidate = (covers_field, covers_label, field_name)
                if best is None or candidate > best:
                    best = candidate
            if best is not None:
                out[best[2]].append(label)

        return {"aliases": {k: sorted(set(v)) for k, v in out.items() if v}}

    def _task_field_schema_classify_criticality(self, ev: dict[str, Any]) -> dict[str, Any]:
        """Propose which fields a reviewer should confirm as critical (DT-08).

        Money and date fields carry the cost of being wrong, so they are
        proposed; the agent still requires explicit confirmation.
        """
        proposed = [
            f["name"]
            for f in ev.get("fields", [])
            if f.get("type") in {"decimal", "date"} or "number" in f["name"].lower()
        ]
        return {"proposed_critical": sorted(proposed)}

    # -- detection rules ---------------------------------------------------

    def _task_detection_rules_weight_terms(self, ev: dict[str, Any]) -> dict[str, Any]:
        """Turn per-term support counts into weights that sum to <= 1."""
        counts: dict[str, int] = ev.get("term_counts", {})
        total = sum(counts.values()) or 1
        weights = {term: round(min(0.45, count / total), 3) for term, count in counts.items()}
        return {"weights": weights}

    def _task_detection_rules_classification_threshold(self, ev: dict[str, Any]) -> dict[str, Any]:
        n_types = max(1, int(ev.get("type_count", 1)))
        base = 0.6 if n_types > 1 else 0.5
        return {"threshold": base}

    # -- skill authoring ---------------------------------------------------

    def _task_skill_author_write_body(self, ev: dict[str, Any]) -> dict[str, Any]:
        skill_id = ev.get("skill_id", "skill")
        email_type = ev.get("email_type", "document")
        fields = ev.get("fields", [])
        aliases = ev.get("aliases", {})
        lines = [
            f"# {skill_id}",
            "",
            f"You are mapping source columns to canonical fields for `{email_type}` documents.",
            "",
            "## Canonical fields",
            "",
        ]
        for name in fields:
            variants = aliases.get(name, [])
            hint = f" — seen as: {', '.join(variants)}" if variants else ""
            lines.append(f"- `{name}`{hint}")
        lines += [
            "",
            "## Rules",
            "",
            "1. Map a source column to a canonical field only when the header or its",
            "   observed variants identify it. The variant list is evidence, not a",
            "   lookup table — judge the column, do not pattern-match blindly.",
            "2. Leave a column unmapped rather than guessing. Report it in `unmapped`.",
            "3. Give each mapping a confidence reflecting how directly the header",
            "   identifies the field.",
            "4. Never invent a value that is not present in the source.",
        ]
        return {"body": "\n".join(lines) + "\n"}

    # -- threshold tuning --------------------------------------------------

    def _task_threshold_tuner_choose_band(self, ev: dict[str, Any]) -> dict[str, Any]:
        """Pick an accept band from the confidence distribution (DT-14).

        Chooses the lowest candidate band whose false-accept rate is within the
        gate, preferring to send more to review rather than accept a bad value.
        """
        candidates = ev.get("candidates", [])
        max_false_accept = float(ev.get("max_false_accept", 0.01))
        viable = [c for c in candidates if c.get("false_accept_rate", 1.0) <= max_false_accept]
        if viable:
            chosen = min(viable, key=lambda c: c["band"])
        elif candidates:
            chosen = max(candidates, key=lambda c: c["band"])
        else:
            chosen = {"band": 0.85, "false_accept_rate": None, "review_rate": None}
        return {"accept_at": chosen["band"], "basis": chosen}

    # -- pattern learning --------------------------------------------------

    def _task_pattern_skill_write(self, ev: dict[str, Any]) -> dict[str, Any]:
        """Write a learned pattern's skill body from the hints the rules derived.

        Adds no hints of its own: the stub has no judgment beyond the rules, so
        the body restates them in prose for a model reading the skill.
        """
        name = ev.get("pattern_name", "pattern")
        obj = ev.get("object") or "record"
        fields = (ev.get("hints") or {}).get("fields") or {}
        lines = [f"## Skill: {name} pattern", "",
                 f'Learned from samples of the "{name}" pattern. Use it when a document '
                 f"follows this layout to find each {obj} field.", ""]
        where = []
        for fname, h in fields.items():
            labels = ", ".join(f'"{x}"' for x in h.get("labels", []))
            if labels:
                where.append(f"- `{fname}` is labelled {labels}.")
            for a in h.get("anchors", []):
                where.append(f'- `{fname}` is the value written just after "{a}".')
            for sub, sh in (h.get("items") or {}).items():
                cols = ", ".join(f'"{x}"' for x in sh.get("labels", []))
                where.append(f"- `{fname}.{sub}` is the column headed {cols}.")
        if where:
            lines += ["### Where the fields are", "", *where, ""]
        missing = [f for f in ev.get("failing_fields", []) if f not in fields]
        if missing:
            lines += ["### Not located yet", "",
                      "These fields failed on the samples and no location was found for them: "
                      + ", ".join(f"`{f}`" for f in missing) + ". Leave them empty rather than guess.", ""]
        ref = (ev.get("reference_text") or "").strip()
        if ref:
            lines += ["### Reference", "", *("> " + x if x.strip() else ">" for x in ref.splitlines()), ""]
        return {"body": "\n".join(lines).rstrip() + "\n", "hints": {}}
