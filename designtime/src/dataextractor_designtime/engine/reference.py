"""A reference runtime engine, used by the evaluation harness.

DT-29 requires the harness to execute the production runtime engine, pinned to
the version in the package's ``engine_range``. That engine is PRD 3's build and
does not exist yet, so this stands in for it: same inputs, same output
contracts, same routing rules read from the package. It implements the Phase 1
path only — classification, body and CSV/XLSX extraction, merge, completeness,
confidence, flag routing — and no PDF, OCR, encryption or embedded email.

Swap this for the real engine behind the same interface and DT-29 is satisfied;
until then, harness numbers describe this engine, not production.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from ..agents import tabular
from ..agents.filetypes import SUPPORTED_PHASE_1, detect
from ..contracts.artifacts import DetectionRules, FieldSchema, Thresholds
from ..contracts.corpus import Sample
from ..contracts.runtime import (
    AgentTraceEntry,
    AuditRecord,
    ExtractedValue,
    ExtractionResult,
    ReviewQueueEntry,
)

ENGINE_NAME = "reference-engine"


@dataclass
class RunOutcome:
    """Exactly one terminal object plus an audit record (CTR-14)."""

    result: ExtractionResult | None
    review: ReviewQueueEntry | None
    audit: AuditRecord

    @property
    def outcome(self) -> str:
        return self.audit.outcome


@dataclass
class _Candidate:
    value: Any
    source: str
    confidence: float
    origin: str  # "body" | "attachment"


class ReferenceEngine:
    def __init__(
        self,
        *,
        schemas: dict[str, FieldSchema],
        detection: DetectionRules,
        thresholds: Thresholds,
        client_id: str,
        workflow_id: str,
        package_version: str,
        engine_version: str,
    ) -> None:
        self.schemas = schemas
        self.detection = detection
        self.thresholds = thresholds
        self.client_id = client_id
        self.workflow_id = workflow_id
        self.package_version = package_version
        self.engine_version = engine_version

    # -- public ------------------------------------------------------------

    def run(self, sample: Sample, corpus_root: Path | None = None) -> RunOutcome:
        audit_id = f"run_{uuid.uuid5(uuid.NAMESPACE_URL, sample.sample_id).hex[:12]}"
        trace: list[AgentTraceEntry] = []
        flags: list[str] = []

        email_type, score = self._classify(f"{sample.subject}\n{sample.body}")
        trace.append(AgentTraceEntry(agent="classification", status="ok" if email_type else "flagged"))
        if email_type is None or email_type not in self.schemas:
            return self._review(
                audit_id, None, 0.0, {}, ["classification_ambiguous"], trace, sample
            )

        schema = self.schemas[email_type]
        candidates: dict[str, list[_Candidate]] = {}

        for name, cand in self._from_body(sample, schema).items():
            candidates.setdefault(name, []).append(cand)
        trace.append(AgentTraceEntry(agent="body_parser"))

        for att in sample.attachments:
            path = self._resolve(att.path, corpus_root)
            if att.unreadable:
                flags.append(f"attachment_parse_failed:{att.filename}")
                trace.append(AgentTraceEntry(agent="attachment_parser", status="flagged"))
                continue
            if path is None:
                continue
            mime = detect(path)
            if mime not in SUPPORTED_PHASE_1:
                flags.append(f"unsupported_attachment:{mime}")
                trace.append(AgentTraceEntry(agent="attachment_router", status="flagged"))
                continue
            try:
                for name, cand in self._from_attachment(path, att.filename, schema).items():
                    candidates.setdefault(name, []).append(cand)
                trace.append(AgentTraceEntry(agent="spreadsheet_parser"))
            except Exception:
                flags.append(f"attachment_parse_failed:{att.filename}")
                trace.append(AgentTraceEntry(agent="spreadsheet_parser", status="failed"))

        merged, conflicts = self._merge(candidates, schema, email_type)
        flags.extend(conflicts)
        trace.append(AgentTraceEntry(agent="merge", status="flagged" if conflicts else "ok"))

        missing = [n for n in schema.required_field_names() if n not in merged]
        flags.extend(f"missing_field:{n}" for n in missing)
        trace.append(AgentTraceEntry(agent="completeness_checker"))

        confidence = self._score(merged, schema)
        trace.append(AgentTraceEntry(agent="confidence_scorer"))

        return self._route(audit_id, email_type, confidence, merged, flags, trace, sample)

    # -- stages ------------------------------------------------------------

    def _classify(self, text: str) -> tuple[str | None, float]:
        lowered = text.lower()
        best: tuple[str | None, float] = (None, 0.0)
        for email_type, rules in self.detection.types.items():
            score = 0.0
            for kw in rules.keywords:
                if kw.term in lowered:
                    score += kw.weight
            for pat in rules.patterns:
                if re.search(pat.regex, text):
                    score += pat.weight
            for neg in rules.negative_signals:
                if neg in lowered:
                    score -= 0.2
            if score >= rules.classification_threshold and score > best[1]:
                best = (email_type, score)
        return best

    def _from_body(self, sample: Sample, schema: FieldSchema) -> dict[str, _Candidate]:
        text = f"{sample.subject}\n{sample.body}"
        segments = [s for s in re.split(r"\n\s*\n", text) if s.strip()] or [text]
        found: dict[str, _Candidate] = {}

        for field in schema.required_fields + schema.optional_fields:
            labels = [field.name.replace("_", " "), field.name] + schema.aliases.get(field.name, [])
            for idx, segment in enumerate(segments):
                hit = self._find_labelled(segment, labels, field.type)
                if hit is not None:
                    found[field.name] = _Candidate(
                        value=hit,
                        source=f"body:segment_{idx}",
                        confidence=0.82,
                        origin="body",
                    )
                    break
        return found

    def _from_attachment(
        self, path: Path, filename: str, schema: FieldSchema
    ) -> dict[str, _Candidate]:
        sheets = tabular.read(path)
        sheet = max(sheets, key=lambda s: (len(s.headers), len(s.rows)))
        if not sheet.headers or not sheet.rows:
            return {}

        found: dict[str, _Candidate] = {}
        for field in schema.required_fields + schema.optional_fields:
            labels = {field.name.lower(), field.name.replace("_", " ").lower()}
            labels |= {a.lower() for a in schema.aliases.get(field.name, [])}
            for col, header in enumerate(sheet.headers):
                if header.strip().lower() in labels:
                    for row_idx, row in enumerate(sheet.rows):
                        if col < len(row) and row[col] not in (None, ""):
                            found[field.name] = _Candidate(
                                value=self._coerce(row[col], field.type),
                                source=sheet.locator(filename, col, row_idx),
                                confidence=0.93,
                                origin="attachment",
                            )
                            break
                    break
        return found

    def _merge(
        self, candidates: dict[str, list[_Candidate]], schema: FieldSchema, email_type: str
    ) -> tuple[dict[str, ExtractedValue], list[str]]:
        """Attachment beats body on agreement; a critical disagreement beyond
        tolerance is flagged rather than resolved (RT-19, RT-20)."""
        tolerances = self.thresholds.types.get(email_type)
        critical = set(schema.critical_field_names())
        merged: dict[str, ExtractedValue] = {}
        conflicts: list[str] = []

        for name, cands in candidates.items():
            if not cands:
                continue
            winner = max(cands, key=lambda c: (c.origin == "attachment", c.confidence))
            others = [c for c in cands if c is not winner]
            for other in others:
                if self._agree(winner.value, other.value):
                    continue
                if name in critical and not self._within_tolerance(
                    name, winner.value, other.value, tolerances
                ):
                    conflicts.append(f"critical_field_conflict:{name}")
                    break
            merged[name] = ExtractedValue(
                value=winner.value, source=winner.source, confidence=winner.confidence
            )
        return merged, sorted(set(conflicts))

    def _score(self, merged: dict[str, ExtractedValue], schema: FieldSchema) -> float:
        required = schema.required_field_names()
        if not required:
            return 0.0
        coverage = sum(1 for n in required if n in merged) / len(required)
        mean_conf = (
            sum(merged[n].confidence for n in required if n in merged) / max(1, len(merged))
        )
        return round(min(1.0, coverage * 0.5 + mean_conf * 0.5), 4)

    def _route(
        self,
        audit_id: str,
        email_type: str,
        confidence: float,
        merged: dict[str, ExtractedValue],
        flags: list[str],
        trace: list[AgentTraceEntry],
        sample: Sample,
    ) -> RunOutcome:
        bands = self.thresholds.types.get(email_type)
        reasons = list(dict.fromkeys(flags))

        if bands is not None:
            # always_review_if conditions are already carried in `flags`; their
            # presence alone forces review regardless of confidence.
            if confidence < bands.accept_at:
                reasons.append("low_confidence")

        trace.append(AgentTraceEntry(agent="flag_router"))
        if reasons:
            return self._review(audit_id, email_type, confidence, merged, reasons, trace, sample)

        result = ExtractionResult(
            audit_id=audit_id,
            client_id=self.client_id,
            workflow_id=self.workflow_id,
            package_version=self.package_version,
            engine_version=self.engine_version,
            type=email_type,
            confidence=confidence,
            fields=merged,
        )
        audit = AuditRecord(
            audit_id=audit_id,
            package_version=self.package_version,
            engine_version=self.engine_version,
            outcome="extracted",
            agent_trace=trace,
            field_attribution=[
                {"field": n, "value": v.value, "source": v.source, "confidence": v.confidence}
                for n, v in merged.items()
            ],
        )
        return RunOutcome(result=result, review=None, audit=audit)

    def _review(
        self,
        audit_id: str,
        email_type: str | None,
        confidence: float,
        merged: dict[str, ExtractedValue],
        reasons: list[str],
        trace: list[AgentTraceEntry],
        sample: Sample,
    ) -> RunOutcome:
        entry = ReviewQueueEntry(
            audit_id=audit_id,
            client_id=self.client_id,
            workflow_id=self.workflow_id,
            package_version=self.package_version,
            type=email_type,
            confidence=confidence,
            partial_fields=merged,
            review_reason=list(dict.fromkeys(reasons)),
            raw_sources=["body"] + [f"attachment:{a.filename}" for a in sample.attachments],
        )
        audit = AuditRecord(
            audit_id=audit_id,
            package_version=self.package_version,
            engine_version=self.engine_version,
            outcome="review_queued",
            agent_trace=trace,
            field_attribution=[
                {"field": n, "value": v.value, "source": v.source, "confidence": v.confidence}
                for n, v in merged.items()
            ],
        )
        return RunOutcome(result=None, review=entry, audit=audit)

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _resolve(path: str | None, root: Path | None) -> Path | None:
        if not path:
            return None
        p = Path(path)
        if not p.is_absolute() and root is not None:
            p = root / p
        return p if p.exists() else None

    @staticmethod
    def _find_labelled(segment: str, labels: list[str], field_type: str) -> Any:
        for label in labels:
            pattern = re.compile(
                rf"{re.escape(label)}\s*[:\-–]\s*(.+?)(?:\n|$)", re.IGNORECASE
            )
            m = pattern.search(segment)
            if m:
                return ReferenceEngine._coerce(m.group(1).strip(), field_type)
        return None

    @staticmethod
    def _coerce(value: Any, field_type: str) -> Any:
        if value is None:
            return None
        if field_type == "decimal":
            if isinstance(value, (int, float)):
                return float(value)
            cleaned = re.sub(r"[^\d.\-]", "", str(value))
            try:
                return float(cleaned)
            except ValueError:
                return str(value)
        if field_type == "integer":
            try:
                return int(re.sub(r"[^\d\-]", "", str(value)))
            except ValueError:
                return str(value)
        if field_type == "date":
            if isinstance(value, datetime):
                return value.date().isoformat()
            if isinstance(value, date):
                return value.isoformat()
            m = re.search(r"\d{4}-\d{2}-\d{2}", str(value))
            return m.group(0) if m else str(value).strip()
        return str(value).strip()

    @staticmethod
    def _agree(a: Any, b: Any) -> bool:
        if isinstance(a, float) and isinstance(b, float):
            return abs(a - b) < 1e-9
        return str(a).strip().lower() == str(b).strip().lower()

    @staticmethod
    def _within_tolerance(name: str, a: Any, b: Any, bands: Any) -> bool:
        if bands is None:
            return False
        tol = bands.critical_field_tolerance.get(name, {})
        if "absolute" in tol and isinstance(a, (int, float)) and isinstance(b, (int, float)):
            return abs(float(a) - float(b)) <= float(tol["absolute"])
        if "days" in tol:
            try:
                return abs((date.fromisoformat(str(a)) - date.fromisoformat(str(b))).days) <= int(
                    tol["days"]
                )
            except ValueError:
                return False
        return False
