"""One route per design agent, so every component is testable on its own.

Each route takes exactly the agent's input model and returns exactly its output
model, which means the OpenAPI schema at /docs is the agent contract.
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
    FieldSchemaAgent,
    FieldSchemaInput,
    FieldSchemaOutput,
    Packager,
    PackagerInput,
    PackagerOutput,
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
    response_model=ProfilerOutput,
    summary="Profile a corpus (DT-06)",
)
def corpus_profiler(payload: ProfilerInput, model: ModelClient = Depends(model_dep)):
    return _run(CorpusProfiler, payload, model)


@router.post(
    "/type-discovery/run",
    response_model=TypeDiscoveryOutput,
    summary="Propose email types for confirmation (DT-07)",
)
def type_discovery(payload: TypeDiscoveryInput, model: ModelClient = Depends(model_dep)):
    return _run(TypeDiscovery, payload, model)


@router.post(
    "/field-schema/run",
    response_model=FieldSchemaOutput,
    summary="Propose a field schema with evidence (DT-08, DT-09)",
)
def field_schema(payload: FieldSchemaInput, model: ModelClient = Depends(model_dep)):
    return _run(FieldSchemaAgent, payload, model)


@router.post(
    "/detection-rules/run",
    response_model=DetectionRulesOutput,
    summary="Generate detection rules that clear the support floor (DT-10, DT-11)",
)
def detection_rules(payload: DetectionRulesInput, model: ModelClient = Depends(model_dep)):
    return _run(DetectionRulesAgent, payload, model)


@router.post(
    "/skill-author/run",
    response_model=SkillAuthorOutput,
    summary="Decide and author skill overrides (DT-12, DT-13)",
)
def skill_author(payload: SkillAuthorInput, model: ModelClient = Depends(model_dep)):
    return _run(SkillAuthorAgent, payload, model)


@router.post(
    "/threshold-tuner/run",
    response_model=ThresholdTunerOutput,
    summary="Derive accept bands from held-out predictions (DT-14)",
)
def threshold_tuner(payload: ThresholdTunerInput, model: ModelClient = Depends(model_dep)):
    return _run(ThresholdTuner, payload, model)


@router.post(
    "/evaluation/run",
    response_model=EvaluationOutput,
    summary="Replay the engine over held-out samples and gate the metrics (DT-23..DT-31)",
)
def evaluation(payload: EvaluationInput, model: ModelClient = Depends(model_dep)):
    return _run(EvaluationAgent, payload, model)


@router.post(
    "/packager/run",
    response_model=PackagerOutput,
    summary="Assemble a package with manifest, checksums and semver bump (DT-15, CTR-17)",
)
def packager(payload: PackagerInput, model: ModelClient = Depends(model_dep)):
    return _run(Packager, payload, model)
