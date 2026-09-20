"""The authoring run (DT-01 .. DT-05).

Chains the eight agents into one addressable job: intake checks, profiling,
type confirmation, artifact generation, a bootstrap evaluation to get a
confidence distribution, threshold tuning, a final gated evaluation, packaging
and publication to the UAT channel.

Every stage is also reachable on its own through the API; this is the
convenience path, not a different implementation.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .agents import (
    CorpusProfiler,
    DetectionRulesAgent,
    DetectionRulesInput,
    EvaluationAgent,
    EvaluationInput,
    FieldSchemaAgent,
    FieldSchemaInput,
    Packager,
    PackagerInput,
    ProfilerInput,
    SkillAuthorAgent,
    SkillAuthorInput,
    SkillBundle,
    ThresholdTuner,
    ThresholdTunerInput,
    TypeDiscovery,
    TypeDiscoveryInput,
)
from .agents.base import ConfirmationRequired, CorpusTooSmall
from .config import get_settings
from .contracts.artifacts import FieldSchema, Thresholds, TypeThresholds
from .contracts.corpus import Corpus
from .model.base import ModelClient


class AuthoringRunInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    corpus: Corpus
    corpus_root: str | None = None
    client_id: str
    workflow_id: str
    #: DT-07: generation does not start until these are confirmed.
    confirmed_types: list[str] = Field(default_factory=list)
    #: DT-08: nothing is critical without an explicit confirmation.
    confirmed_critical: dict[str, list[str]] = Field(default_factory=dict)
    reviewed_by: str | None = None
    previous_version: str | None = None
    previous_artifacts: dict[str, Any] | None = None
    #: Skip the intake floor for a small demonstration corpus.
    enforce_corpus_floor: bool = True
    baseline_metrics: dict[str, float] = Field(default_factory=dict)


class StageRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stage: str
    agent: str
    summary: dict[str, Any] = Field(default_factory=dict)


class AuthoringRunOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_id: str
    workflow_id: str
    version: str
    stages: list[StageRecord] = Field(default_factory=list)
    manifest: dict[str, Any]
    artifacts: dict[str, Any]
    bodies: dict[str, str]
    eval_report: dict[str, Any]
    passed: bool
    gate_failed: bool
    heldout_ids: list[str] = Field(default_factory=list)


def check_intake(corpus: Corpus) -> None:
    """DT-18: minimum viable size per type, with the shortfall named."""
    settings = get_settings()
    held = corpus.heldout_ids(settings.heldout_fraction)
    shortfalls: dict[str, dict[str, int]] = {}
    for email_type in sorted(corpus.types()):
        ids = corpus.samples_of_type(email_type)
        held_here = [i for i in ids if i in held]
        if len(ids) < settings.corpus_min_per_type or len(held_here) < settings.corpus_min_heldout:
            shortfalls[email_type] = {
                "have": len(ids),
                "need": settings.corpus_min_per_type,
                "heldout_have": len(held_here),
                "heldout_need": settings.corpus_min_heldout,
            }
    if shortfalls:
        raise CorpusTooSmall(
            "corpus below the intake floor (DT-18)", detail=shortfalls
        )


def run_authoring(payload: AuthoringRunInput, model: ModelClient | None = None) -> AuthoringRunOutput:
    settings = get_settings()
    corpus = payload.corpus
    stages: list[StageRecord] = []

    if payload.enforce_corpus_floor:
        check_intake(corpus)

    profiler = CorpusProfiler(model=model)
    profile = profiler.run(ProfilerInput(corpus=corpus, corpus_root=payload.corpus_root))
    stages.append(
        StageRecord(
            stage="profile",
            agent=profiler.identity,
            summary={
                "samples": profile.sample_count,
                "attachment_kinds": profile.attachment_kinds,
                "unreadable": len(profile.unreadable),
                "unsupported": len(profile.unsupported),
                "column_labels": len(profile.observed_column_labels),
            },
        )
    )

    discovery = TypeDiscovery(model=model)
    discovered = discovery.run(TypeDiscoveryInput(corpus=corpus))
    proposed = [p.email_type for p in discovered.proposals]
    stages.append(
        StageRecord(
            stage="type_discovery",
            agent=discovery.identity,
            summary={"proposed": proposed, "requires_confirmation": True},
        )
    )

    if not payload.confirmed_types:
        raise ConfirmationRequired(
            "type list must be confirmed before generation (DT-07)", detail={"proposed": proposed}
        )
    unknown = sorted(set(payload.confirmed_types) - set(proposed))
    if unknown:
        raise ConfirmationRequired(
            "confirmed types not present in the corpus", detail={"unknown": unknown}
        )

    held = sorted(corpus.heldout_ids(settings.heldout_fraction))

    schemas: dict[str, FieldSchema] = {}
    for email_type in payload.confirmed_types:
        agent = FieldSchemaAgent(model=model)
        out = agent.run(
            FieldSchemaInput(
                corpus=corpus,
                email_type=email_type,
                observed_column_labels=profile.observed_column_labels,
                confirmed_critical=payload.confirmed_critical.get(email_type, []),
                exclude_sample_ids=held,
            )
        )
        schemas[email_type] = out.artifact
        stages.append(
            StageRecord(
                stage=f"field_schema:{email_type}",
                agent=agent.identity,
                summary={
                    "required": [f.name for f in out.artifact.required_fields],
                    "optional": [f.name for f in out.artifact.optional_fields],
                    "aliases": {k: len(v) for k, v in out.artifact.aliases.items()},
                },
            )
        )

    rules_agent = DetectionRulesAgent(model=model)
    rules = rules_agent.run(
        DetectionRulesInput(
            corpus=corpus, email_types=payload.confirmed_types, exclude_sample_ids=held
        )
    )
    stages.append(
        StageRecord(
            stage="detection_rules",
            agent=rules_agent.identity,
            summary={
                "kept": len(rules.evidence),
                "dropped": len(rules.dropped),
                "types": sorted(rules.artifact.types),
            },
        )
    )

    skills: list[SkillBundle] = []
    for email_type in payload.confirmed_types:
        author = SkillAuthorAgent(model=model)
        authored = author.run(
            SkillAuthorInput(
                email_type=email_type,
                field_schema=schemas[email_type],
                baseline_metrics=payload.baseline_metrics,
                sheet_names=profile.observed_sheet_names,
            )
        )
        skills.extend(
            SkillBundle(manifest=s.manifest, body=s.body) for s in authored.skills
        )
        stages.append(
            StageRecord(
                stage=f"skill_author:{email_type}",
                agent=author.identity,
                summary={
                    d.skill_id: ("override" if d.override else "inherit default")
                    for d in authored.decisions
                },
            )
        )

    # Bootstrap thresholds accept everything, so pass 1 yields a confidence
    # distribution rather than a routing decision (DT-14).
    bootstrap = Thresholds(
        types={t: TypeThresholds(accept_at=0.0, review_below=0.0, reject_below=0.0, always_review_if=[]) for t in payload.confirmed_types},
        generated_by="bootstrap",
    )
    evaluator = EvaluationAgent(model=model)
    pass1 = evaluator.run(
        EvaluationInput(
            corpus=corpus,
            corpus_root=payload.corpus_root,
            schemas=schemas,
            detection=rules.artifact,
            thresholds=bootstrap,
            client_id=payload.client_id,
            workflow_id=payload.workflow_id,
        )
    )
    stages.append(
        StageRecord(
            stage="evaluation:bootstrap",
            agent=evaluator.identity,
            summary={"predictions": len(pass1.predictions), "metrics": pass1.metrics.model_dump()},
        )
    )

    tuner = ThresholdTuner(model=model)
    # Only numeric and date criticals get a tolerance; a string critical field
    # has to match exactly, and an empty entry would read as "anything goes".
    tolerance = {
        t: {
            name: band
            for name, band in (
                (n, _tolerance_for(schemas[t], n)) for n in schemas[t].critical_field_names()
            )
            if band is not None
        }
        for t in payload.confirmed_types
    }
    tuned = tuner.run(
        ThresholdTunerInput(predictions=pass1.predictions, critical_field_tolerance=tolerance)
    )
    stages.append(
        StageRecord(
            stage="threshold_tuning",
            agent=tuner.identity,
            summary={t.email_type: t.chosen_band for t in tuned.tuning},
        )
    )

    final = evaluator.run(
        EvaluationInput(
            corpus=corpus,
            corpus_root=payload.corpus_root,
            schemas=schemas,
            detection=rules.artifact,
            thresholds=tuned.artifact,
            client_id=payload.client_id,
            workflow_id=payload.workflow_id,
        )
    )
    stages.append(
        StageRecord(
            stage="evaluation:final",
            agent=evaluator.identity,
            summary={"metrics": final.metrics.model_dump(), "passed": final.passed},
        )
    )

    packager = Packager(model=model)
    package = packager.run(
        PackagerInput(
            client_id=payload.client_id,
            workflow_id=payload.workflow_id,
            source_corpus_id=corpus.meta.corpus_id,
            schemas=schemas,
            detection=rules.artifact,
            thresholds=tuned.artifact,
            skills=skills,
            eval_report=final.report,
            previous_version=payload.previous_version,
            previous_artifacts=payload.previous_artifacts,
            reviewed_by=payload.reviewed_by,
            email_types=payload.confirmed_types,
        )
    )
    stages.append(
        StageRecord(
            stage="package",
            agent=packager.identity,
            summary={
                "version": package.manifest.version,
                "required_bump": package.required_bump,
                "artifacts": len(package.manifest.artifacts),
            },
        )
    )

    return AuthoringRunOutput(
        client_id=payload.client_id,
        workflow_id=payload.workflow_id,
        version=package.manifest.version,
        stages=stages,
        manifest=package.manifest.model_dump(mode="json"),
        artifacts=package.artifacts,
        bodies=package.bodies,
        eval_report=final.report,
        passed=final.passed,
        gate_failed=package.gate_failed,
        heldout_ids=held,
    )


def _tolerance_for(schema: FieldSchema, name: str) -> dict[str, float] | None:
    for f in schema.required_fields + schema.optional_fields:
        if f.name != name:
            continue
        if f.type in {"decimal", "integer"}:
            return {"absolute": 0.01}
        if f.type == "date":
            return {"days": 0}
        return None
    return None
