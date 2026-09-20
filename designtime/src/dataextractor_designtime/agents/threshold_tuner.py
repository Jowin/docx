"""Threshold Tuner (DT-14).

Derives accept bands from the confidence distribution of correct versus
incorrect predictions on held-out samples, and reports the trade-off at every
candidate band so the designer sees what each one costs in review volume.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from ..contracts.artifacts import Thresholds, TypeThresholds
from ..model.base import ModelRequest
from .base import DesignAgent

DEFAULT_CANDIDATES = [0.50, 0.60, 0.70, 0.75, 0.80, 0.85, 0.90, 0.95]


class Prediction(BaseModel):
    """One held-out prediction, as the evaluation harness reports it."""

    model_config = ConfigDict(extra="forbid")

    sample_id: str
    email_type: str
    confidence: float
    #: True when every critical field matched ground truth.
    critical_correct: bool
    #: True when every required field matched ground truth.
    all_correct: bool


class BandTradeoff(BaseModel):
    model_config = ConfigDict(extra="forbid")

    band: float
    accepted: int
    review_rate: float
    false_accept_rate: float
    residual_error_above_band: float


class ThresholdTunerInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    predictions: list[Prediction] = Field(default_factory=list)
    candidates: list[float] = Field(default_factory=lambda: list(DEFAULT_CANDIDATES))
    #: DT-27 platform floor; a band above this false-accept rate is not viable.
    max_false_accept: float = 0.01
    critical_field_tolerance: dict[str, dict[str, dict[str, float]]] = Field(default_factory=dict)
    always_review_if: list[str] = Field(
        default_factory=lambda: [
            "encrypted_no_key",
            "attachment_parse_failed",
            "embedded_depth_exceeded",
        ]
    )


class TypeTuning(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email_type: str
    chosen_band: float
    tradeoffs: list[BandTradeoff] = Field(default_factory=list)
    basis: dict[str, float | None] = Field(default_factory=dict)


class ThresholdTunerOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    artifact: Thresholds
    tuning: list[TypeTuning] = Field(default_factory=list)


class ThresholdTuner(DesignAgent[ThresholdTunerInput, ThresholdTunerOutput]):
    name = "threshold-tuner"

    def run(self, payload: ThresholdTunerInput) -> ThresholdTunerOutput:
        by_type: dict[str, list[Prediction]] = {}
        for pred in payload.predictions:
            by_type.setdefault(pred.email_type, []).append(pred)

        tuning: list[TypeTuning] = []
        types: dict[str, TypeThresholds] = {}

        for email_type, preds in sorted(by_type.items()):
            tradeoffs = [self._tradeoff(band, preds) for band in sorted(payload.candidates)]
            chosen = self.model.complete(
                ModelRequest(
                    task="threshold_tuner.choose_band",
                    evidence={
                        "candidates": [t.model_dump() for t in tradeoffs],
                        "max_false_accept": payload.max_false_accept,
                    },
                )
            ).output

            band = float(chosen["accept_at"])
            types[email_type] = TypeThresholds(
                accept_at=band,
                review_below=band,
                reject_below=round(max(0.0, band - 0.45), 4),
                required_field_coverage=1.0,
                critical_field_tolerance=payload.critical_field_tolerance.get(email_type, {}),
                always_review_if=list(payload.always_review_if),
            )
            tuning.append(
                TypeTuning(
                    email_type=email_type,
                    chosen_band=band,
                    tradeoffs=tradeoffs,
                    basis={k: v for k, v in chosen.get("basis", {}).items() if isinstance(v, (int, float, type(None)))},
                )
            )

        return ThresholdTunerOutput(
            artifact=Thresholds(types=types, generated_by=self.identity),
            tuning=tuning,
        )

    @staticmethod
    def _tradeoff(band: float, preds: list[Prediction]) -> BandTradeoff:
        total = len(preds)
        accepted = [p for p in preds if p.confidence >= band]
        bad_accepts = [p for p in accepted if not p.critical_correct]
        wrong_accepts = [p for p in accepted if not p.all_correct]
        return BandTradeoff(
            band=band,
            accepted=len(accepted),
            review_rate=round((total - len(accepted)) / total, 4) if total else 0.0,
            false_accept_rate=round(len(bad_accepts) / total, 4) if total else 0.0,
            residual_error_above_band=round(len(wrong_accepts) / len(accepted), 4) if accepted else 0.0,
        )
