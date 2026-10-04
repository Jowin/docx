"""Evaluation Agent (DT-23 .. DT-30, DT-35).

Replays the held-out corpus through an engine with the candidate config and
reports the gated metrics, a per-field error breakdown and failure exemplars.
Deterministic: the same config over the same corpus produces the same report
hash (DT-35).

``engine`` picks what extracts (DT-29):

* ``runtime`` (the default): the real runtime, in an isolated process, with
  the candidate written as a runtime config version folder (engine/runtime.py).
  Give ``config_folder`` to evaluate a folder already written (the authoring
  run does, so what is scored is what is published); otherwise the folder is
  written from ``schemas``, ``detection``, ``thresholds`` and ``skills``.
  Out-of-scope samples are run too and reported as ``scope`` (were they
  turned away?).
* ``reference``: the in-process reference engine, a deterministic stand-in kept
  for quick component tests; it is not the engine that serves.
"""

from __future__ import annotations

import re
import tempfile
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from ..records import Record
from ..config import get_settings
from ..contracts.artifacts import DetectionRules, FieldSchema, Thresholds, sha256_of
from ..contracts.corpus import Corpus
from ..engine.reference import ReferenceEngine
from .base import DesignAgent
from .threshold_tuner import Prediction

#: DT-23 .. DT-28 default gates.
DEFAULT_GATES: dict[str, float] = {
    "type_accuracy": 0.95,
    "field_accuracy": 0.90,
    "critical_field_accuracy": 0.95,
    "source_attribution_accuracy": 0.85,
    "review_rate": 0.20,
    "false_accept_rate": 0.01,
    "calibration_gap": 0.10,
}
#: DT-31: the platform floor that per-client config may not loosen.
FALSE_ACCEPT_FLOOR = 0.01


_ROWCOL = re.compile(r"^row(\d+)col(\d+)$", re.I)


def _col_letters(n: int) -> str:
    out = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        out = chr(65 + r) + out
    return out


def canonical_source(locator: str | None) -> tuple[str, str] | None:
    """One spelling for a cell locator: (file, "SHEET!B2" | "B2" | rest).

    Both CTR-13 spellings meet here: the runtime's ``attachment:inv.xlsx#Summary!B2``
    and ``attachment:inv.csv#B2``, and the corpus' ``attachment:inv.xlsx!Summary!B2``
    and ``attachment:inv.csv#row2col2``.
    """
    if not locator:
        return None
    text = str(locator).strip()
    for prefix in ("attachment:", "file:"):
        if text.startswith(prefix):
            text = text[len(prefix):]
    if "#" in text:
        name, rest = text.split("#", 1)
    elif "!" in text:
        name, rest = text.split("!", 1)
    else:
        return text.lower(), ""
    m = _ROWCOL.match(rest)
    if m:
        rest = f"{_col_letters(int(m.group(2)))}{int(m.group(1))}"
    return name.lower(), rest.upper()


def same_source(got: str | None, expected: str | None) -> bool:
    a, b = canonical_source(got), canonical_source(expected)
    return a is not None and a == b


@dataclass(kw_only=True)
class FieldBreakdown(Record):
    field: str
    expected: int
    correct: int
    accuracy: float
    critical: bool


@dataclass(kw_only=True)
class FailureExemplar(Record):
    sample_id: str
    audit_id: str
    email_type: str | None
    outcome: str
    reasons: list[str] = field(default_factory=list)
    wrong_fields: list[str] = field(default_factory=list)


@dataclass(kw_only=True)
class Metrics(Record):
    type_accuracy: float = 0.0
    field_accuracy: float = 0.0
    critical_field_accuracy: float = 0.0
    source_attribution_accuracy: float | None = None
    review_rate: float = 0.0
    false_accept_rate: float = 0.0
    calibration_gap: float = 0.0


@dataclass(kw_only=True)
class EvaluationInput(Record):
    corpus: Corpus
    corpus_root: str | None = None
    schemas: dict[str, FieldSchema]
    detection: DetectionRules
    thresholds: Thresholds
    client_id: str
    workflow_id: str
    package_version: str = "0.0.0"
    engine_version: str | None = None
    gates: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_GATES))
    #: Evaluate the held-out split only (DT-22); false evaluates everything.
    heldout_only: bool = True
    #: runtime = the real runtime on a config folder (DT-29); reference = the in-process stand-in.
    engine: Literal["runtime", "reference"] = "runtime"
    #: An already-written runtime config version folder to evaluate (engine=runtime).
    config_folder: str | None = None
    #: Authored skills to write into the folder when it is built here (SkillBundle dicts).
    skills: list[dict[str, Any]] = field(default_factory=list)


@dataclass(kw_only=True)
class EvaluationOutput(Record):
    report: dict[str, Any]
    report_sha256: str
    metrics: Metrics
    gates: dict[str, float]
    gate_results: dict[str, bool]
    passed: bool
    predictions: list[Prediction] = field(default_factory=list)
    field_breakdown: list[FieldBreakdown] = field(default_factory=list)
    failures: list[FailureExemplar] = field(default_factory=list)
    #: Samples excluded from source-attribution scoring for lack of a locator (DT-19).
    attribution_excluded: list[str] = field(default_factory=list)


class EvaluationAgent(DesignAgent[EvaluationInput, EvaluationOutput]):
    name = "evaluation"

    def run(self, payload: EvaluationInput) -> EvaluationOutput:
        settings = get_settings()
        engine_version = payload.engine_version or settings.engine_version
        gates = {**DEFAULT_GATES, **payload.gates}
        if gates["false_accept_rate"] > FALSE_ACCEPT_FLOOR:
            raise ValueError(
                f"false_accept_rate gate {gates['false_accept_rate']} exceeds the "
                f"platform floor {FALSE_ACCEPT_FLOOR} (DT-31)"
            )

        corpus = payload.corpus
        root = Path(payload.corpus_root) if payload.corpus_root else None
        reference = ReferenceEngine(
            schemas=payload.schemas,
            detection=payload.detection,
            thresholds=payload.thresholds,
            client_id=payload.client_id,
            workflow_id=payload.workflow_id,
            package_version=payload.package_version,
            engine_version=engine_version,
        )

        held = corpus.heldout_ids(settings.heldout_fraction) if payload.heldout_only else None
        labels = corpus.label_index()
        targets = [
            s for s in corpus.samples
            if s.sample_id in labels
            and labels[s.sample_id].in_scope
            and (held is None or s.sample_id in held)
        ]
        targets.sort(key=lambda s: s.sample_id)
        held_out = corpus.heldout_out_of_scope(settings.heldout_fraction) if payload.heldout_only else None
        outside = sorted((s for s in corpus.samples if s.sample_id in labels and not labels[s.sample_id].in_scope
                          and (held_out is None or s.sample_id in held_out)), key=lambda s: s.sample_id)
        if payload.engine == "runtime":
            outcomes = self._runtime_outcomes(payload, settings, targets + outside, root)
            run = lambda sample: outcomes[sample.sample_id]       # noqa: E731
        else:
            run = lambda sample: reference.run(sample, root)      # noqa: E731
            outside = []

        type_hits = 0
        reviewed = 0
        false_accepts = 0
        field_expected: dict[str, int] = defaultdict(int)
        field_correct: dict[str, int] = defaultdict(int)
        attribution_expected = 0
        attribution_correct = 0
        attribution_excluded: list[str] = []
        calibration: list[tuple[float, bool]] = []
        predictions: list[Prediction] = []
        failures: list[FailureExemplar] = []
        critical_fields: set[str] = {
            name
            for schema in payload.schemas.values()
            for name in schema.critical_field_names()
        }

        for sample in targets:
            label = labels[sample.sample_id]
            outcome = run(sample)
            produced = outcome.result or outcome.review
            observed_type = produced.type if produced else None
            if observed_type == label.email_type:
                type_hits += 1

            fields = (
                outcome.result.fields if outcome.result else (outcome.review.partial_fields if outcome.review else {})
            )
            schema = payload.schemas.get(label.email_type)
            required = set(schema.required_field_names()) if schema else set(label.fields)

            wrong: list[str] = []
            all_correct = True
            critical_correct = True
            for name in sorted(required):
                if name not in label.fields:
                    continue
                field_expected[name] += 1
                got = fields.get(name)
                if got is not None and self._equal(got.value, label.fields[name]):
                    field_correct[name] += 1
                else:
                    wrong.append(name)
                    all_correct = False
                    if name in critical_fields:
                        critical_correct = False

                if name in label.field_sources:
                    attribution_expected += 1
                    if got is not None and same_source(got.source, label.field_sources[name]):
                        attribution_correct += 1
                elif got is not None:
                    attribution_excluded.append(f"{sample.sample_id}:{name}")

            confidence = produced.confidence if produced else 0.0
            predictions.append(
                Prediction(
                    sample_id=sample.sample_id,
                    email_type=label.email_type,
                    confidence=confidence,
                    critical_correct=critical_correct,
                    all_correct=all_correct,
                )
            )
            calibration.append((confidence, all_correct))

            if outcome.review is not None:
                reviewed += 1
                failures.append(
                    FailureExemplar(
                        sample_id=sample.sample_id,
                        audit_id=outcome.audit.audit_id,
                        email_type=observed_type,
                        outcome="review_queued",
                        reasons=outcome.review.review_reason,
                        wrong_fields=wrong,
                    )
                )
            else:
                if not critical_correct:
                    false_accepts += 1
                    failures.append(
                        FailureExemplar(
                            sample_id=sample.sample_id,
                            audit_id=outcome.audit.audit_id,
                            email_type=observed_type,
                            outcome="false_accept",
                            wrong_fields=wrong,
                        )
                    )

        total = len(targets) or 1
        expected_total = sum(field_expected.values()) or 1
        correct_total = sum(field_correct.values())
        critical_expected = sum(field_expected[f] for f in critical_fields) or 1
        critical_correct_total = sum(field_correct[f] for f in critical_fields)

        metrics = Metrics(
            type_accuracy=round(type_hits / total, 4),
            field_accuracy=round(correct_total / expected_total, 4),
            critical_field_accuracy=round(critical_correct_total / critical_expected, 4),
            source_attribution_accuracy=(
                round(attribution_correct / attribution_expected, 4) if attribution_expected else None
            ),
            review_rate=round(reviewed / total, 4),
            false_accept_rate=round(false_accepts / total, 4),
            calibration_gap=self._calibration_gap(calibration),
        )

        gate_results = {
            "type_accuracy": metrics.type_accuracy >= gates["type_accuracy"],
            "field_accuracy": metrics.field_accuracy >= gates["field_accuracy"],
            "critical_field_accuracy": metrics.critical_field_accuracy >= gates["critical_field_accuracy"],
            "source_attribution_accuracy": (
                metrics.source_attribution_accuracy is None
                or metrics.source_attribution_accuracy >= gates["source_attribution_accuracy"]
            ),
            "review_rate": metrics.review_rate <= gates["review_rate"],
            "false_accept_rate": metrics.false_accept_rate <= gates["false_accept_rate"],
            "calibration_gap": metrics.calibration_gap <= gates["calibration_gap"],
        }

        breakdown = [
            FieldBreakdown(
                field=name,
                expected=field_expected[name],
                correct=field_correct[name],
                accuracy=round(field_correct[name] / field_expected[name], 4),
                critical=name in critical_fields,
            )
            for name in sorted(field_expected)
        ]

        turned_away = [s.sample_id for s in outside
                       if getattr(run(s), "classification", {}).get("status") == "out_of_scope"]
        report = {
            "engine": payload.engine,
            "scope": {"out_of_scope_samples": len(outside), "turned_away": len(turned_away),
                      "accepted_by_mistake": sorted(set(s.sample_id for s in outside) - set(turned_away))},
            "corpus_id": corpus.meta.corpus_id,
            "corpus_size": len(targets),
            "heldout_only": payload.heldout_only,
            "engine_version": engine_version,
            "package_version": payload.package_version,
            "metrics": metrics.to_dict(),
            "gates": gates,
            "gate_results": gate_results,
            "field_breakdown": [b.to_dict() for b in breakdown],
            "failures": [f.to_dict() for f in failures],
            "attribution_excluded": sorted(attribution_excluded),
            "generated_by": self.identity,
        }

        return EvaluationOutput(
            report=report,
            report_sha256=sha256_of(report),
            metrics=metrics,
            gates=gates,
            gate_results=gate_results,
            passed=all(gate_results.values()),
            predictions=predictions,
            field_breakdown=breakdown,
            failures=failures,
            attribution_excluded=sorted(attribution_excluded),
        )

    def _runtime_outcomes(self, payload: EvaluationInput, settings, samples, root):
        from .. import configwriter
        from ..engine.runtime import RuntimeEngine
        from .packager import SkillBundle

        if payload.config_folder:
            engine = RuntimeEngine(settings, client=payload.client_id, usecase=payload.workflow_id,
                                   folder=Path(payload.config_folder))
            return engine.run(samples, root)
        base, _ = configwriter.base_folder(settings.runtime_config_root, payload.client_id, payload.workflow_id)
        with tempfile.TemporaryDirectory(prefix="dt-eval-cfg-") as t:
            folder = configwriter.write_version(
                Path(t) / "candidate", client=payload.client_id, usecase=payload.workflow_id,
                schemas=payload.schemas, detection=payload.detection, thresholds=payload.thresholds,
                skills=[SkillBundle.from_dict(b) for b in payload.skills], base=base)
            engine = RuntimeEngine(settings, client=payload.client_id, usecase=payload.workflow_id, folder=folder)
            return engine.run(samples, root)

    @staticmethod
    def _equal(got: Any, expected: Any) -> bool:
        if isinstance(got, (int, float)) or isinstance(expected, (int, float)):
            try:                           # a runtime may write decimals as strings ("decimal_format")
                return abs(float(str(got).replace(",", "")) - float(str(expected).replace(",", ""))) < 0.005
            except ValueError:
                pass
        return str(got).strip().lower() == str(expected).strip().lower()

    @staticmethod
    def _calibration_gap(pairs: list[tuple[float, bool]], buckets: int = 5) -> float:
        """DT-28: mean absolute gap between stated confidence and observed
        accuracy, bucketed."""
        if not pairs:
            return 0.0
        binned: dict[int, list[tuple[float, bool]]] = defaultdict(list)
        for conf, ok in pairs:
            binned[min(buckets - 1, int(conf * buckets))].append((conf, ok))
        gaps = []
        for items in binned.values():
            mean_conf = sum(c for c, _ in items) / len(items)
            observed = sum(1 for _, ok in items if ok) / len(items)
            gaps.append(abs(mean_conf - observed))
        return round(sum(gaps) / len(gaps), 4)
