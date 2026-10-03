"""One route per design agent, so every component is testable on its own.

Each route takes exactly the agent's input record and returns exactly its output
record; both schemas are published at /docs, so OpenAPI is the agent contract.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from ...agents import (
    CorpusProfiler,
    DetectionRulesAgent,
    DetectionRulesInput,
    DetectionRulesOutput,
    EvaluationAgent,
    EvaluationInput,
    EvaluationOutput,
    ExtractionJudge,
    ExtractionJudgeInput,
    ExtractionJudgeOutput,
    FieldSchemaAgent,
    FieldSchemaInput,
    FieldSchemaOutput,
    Packager,
    PackagerInput,
    PackagerOutput,
    PatternSkillWriter,
    PatternSkillWriterInput,
    PatternSkillWriterOutput,
    ProfilerInput,
    ProfilerOutput,
    SkillAuthorAgent,
    SkillAuthorInput,
    SkillAuthorOutput,
    ThresholdTuner,
    ThresholdTunerInput,
    ThresholdTunerOutput,
    TypeDiscovery,
    TypeDiscoveryInput,
    TypeDiscoveryOutput,
)
from ...agents.base import AgentError
from ...model.base import ModelClient
from ..deps import as_http, model_dep
from ..typed import docs, json_body, out

router = APIRouter(prefix="/agents", tags=["agents"])


def _run(agent_cls, payload, model: ModelClient):
    try:
        return agent_cls(model=model).run(payload)
    except AgentError as exc:
        raise as_http(exc) from exc
    except ValueError as exc:
        raise as_http(AgentError(str(exc), code="invalid_input")) from exc


@router.post(
    "/corpus-profiler/run",
    summary="Profile a corpus (DT-06)",
    **docs(ProfilerInput, ProfilerOutput),
)
def corpus_profiler(
    payload: ProfilerInput = Depends(json_body(ProfilerInput)),
    model: ModelClient = Depends(model_dep),
):
    return out(_run(CorpusProfiler, payload, model))


@router.post(
    "/type-discovery/run",
    summary="Propose email types for confirmation (DT-07)",
    **docs(TypeDiscoveryInput, TypeDiscoveryOutput),
)
def type_discovery(
    payload: TypeDiscoveryInput = Depends(json_body(TypeDiscoveryInput)),
    model: ModelClient = Depends(model_dep),
):
    return out(_run(TypeDiscovery, payload, model))


@router.post(
    "/field-schema/run",
    summary="Propose a field schema with evidence (DT-08, DT-09)",
    **docs(FieldSchemaInput, FieldSchemaOutput),
)
def field_schema(
    payload: FieldSchemaInput = Depends(json_body(FieldSchemaInput)),
    model: ModelClient = Depends(model_dep),
):
    return out(_run(FieldSchemaAgent, payload, model))


@router.post(
    "/detection-rules/run",
    summary="Generate detection rules that clear the support floor (DT-10, DT-11)",
    **docs(DetectionRulesInput, DetectionRulesOutput),
)
def detection_rules(
    payload: DetectionRulesInput = Depends(json_body(DetectionRulesInput)),
    model: ModelClient = Depends(model_dep),
):
    return out(_run(DetectionRulesAgent, payload, model))


@router.post(
    "/skill-author/run",
    summary="Decide and author skill overrides (DT-12, DT-13)",
    **docs(SkillAuthorInput, SkillAuthorOutput),
)
def skill_author(
    payload: SkillAuthorInput = Depends(json_body(SkillAuthorInput)),
    model: ModelClient = Depends(model_dep),
):
    return out(_run(SkillAuthorAgent, payload, model))


@router.post(
    "/threshold-tuner/run",
    summary="Derive accept bands from held-out predictions (DT-14)",
    **docs(ThresholdTunerInput, ThresholdTunerOutput),
)
def threshold_tuner(
    payload: ThresholdTunerInput = Depends(json_body(ThresholdTunerInput)),
    model: ModelClient = Depends(model_dep),
):
    return out(_run(ThresholdTuner, payload, model))


@router.post(
    "/evaluation/run",
    summary="Replay the engine over held-out samples and gate the metrics (DT-23..DT-31)",
    **docs(EvaluationInput, EvaluationOutput),
)
def evaluation(
    payload: EvaluationInput = Depends(json_body(EvaluationInput)),
    model: ModelClient = Depends(model_dep),
):
    return out(_run(EvaluationAgent, payload, model))


@router.post(
    "/packager/run",
    summary="Assemble a package with manifest, checksums and semver bump (DT-15, CTR-17)",
    **docs(PackagerInput, PackagerOutput),
)
def packager(
    payload: PackagerInput = Depends(json_body(PackagerInput)),
    model: ModelClient = Depends(model_dep),
):
    return out(_run(Packager, payload, model))


@router.post(
    "/extraction-judge/run",
    summary="Judge one runtime extraction against ground truth, or its review status",
    **docs(ExtractionJudgeInput, ExtractionJudgeOutput),
)
def extraction_judge(
    payload: ExtractionJudgeInput = Depends(json_body(ExtractionJudgeInput)),
    model: ModelClient = Depends(model_dep),
):
    return out(_run(ExtractionJudge, payload, model))


@router.post(
    "/pattern-skill-writer/run",
    summary="Write a learned skill (hints + body) for a failing document pattern",
    **docs(PatternSkillWriterInput, PatternSkillWriterOutput),
)
def pattern_skill_writer(
    payload: PatternSkillWriterInput = Depends(json_body(PatternSkillWriterInput)),
    model: ModelClient = Depends(model_dep),
):
    return out(_run(PatternSkillWriter, payload, model))
