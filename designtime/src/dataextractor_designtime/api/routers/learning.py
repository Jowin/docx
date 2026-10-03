"""Pattern learning: one route that learns from a sample, two that read the record."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, Request

from ...agents.base import AgentError
from ...config import Settings
from ...learning import IsolatedRuntime, LearnRequest, LearnResponse, LearnRunSummary, LearningStore, learn
from ...model.base import ModelClient
from ...records import ValidationError
from ..deps import as_http, model_dep, session_dep, settings_dep
from ..typed import docs, json_body, out

router = APIRouter(prefix="/learning", tags=["learning"])


def runtime_dep(request: Request, settings: Settings = Depends(settings_dep)) -> IsolatedRuntime:
    """The isolated runtime; a test or deployment may set ``app.state.isolated_runtime``."""
    override = getattr(request.app.state, "isolated_runtime", None)
    if override is not None:
        return override
    try:
        return IsolatedRuntime(settings.runtime_dir, python=settings.runtime_python,
                               input_root=settings.learning_input_root, timeout_s=settings.runtime_timeout_s,
                               model_provider=settings.learning_model_provider)
    except AgentError as exc:
        raise as_http(exc) from exc


def store_dep(session=Depends(session_dep)) -> LearningStore:
    return LearningStore(session)


def _summary(row) -> LearnRunSummary:
    return LearnRunSummary(id=row.id, created_at=row.created_at.isoformat(), client=row.client_id,
                           usecase=row.usecase, object=row.object_name, pattern_name=row.pattern_name,
                           source=row.source, outcome=row.outcome, base_version=row.base_version,
                           result_version=row.result_version, passed_before=row.passed_before,
                           passed_after=row.passed_after, requested_by=row.requested_by)


@router.post(
    "/runs",
    summary="Extract a sample in the isolated runtime; when it fails, learn a skill for its pattern",
    **docs(LearnRequest, LearnResponse),
)
def learn_from_sample(
    payload: LearnRequest = Depends(json_body(LearnRequest)),
    runtime: IsolatedRuntime = Depends(runtime_dep),
    store: LearningStore = Depends(store_dep),
    model: ModelClient = Depends(model_dep),
    settings: Settings = Depends(settings_dep),
):
    try:
        result = learn(payload, runtime=runtime, store=store, model=model,
                       config_root=settings.runtime_config_root,
                       max_iterations=settings.learning_max_iterations,
                       regression_limit=settings.learning_regression_samples)
    except (AgentError, ValidationError) as exc:
        raise as_http(exc) from exc
    return out(result)


@router.get("/runs", summary="Learning runs, newest first", **docs(response=LearnRunSummary, many=True))
def list_runs(
    client: str | None = Query(default=None),
    usecase: str | None = Query(default=None),
    pattern_name: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    store: LearningStore = Depends(store_dep),
):
    return out([_summary(r) for r in store.list(client, usecase, pattern_name, limit)])


@router.get("/runs/{run_id}", summary="One learning run as recorded, attempts and skill included")
def get_run(run_id: str, store: LearningStore = Depends(store_dep)) -> Any:
    try:
        row = store.get(run_id)
    except AgentError as exc:
        raise as_http(exc) from exc
    return {**out(_summary(row)), "source_sha256": row.source_sha256, "ground_truth": row.ground_truth,
            "reference_text": row.reference_text, "verdict_before": row.verdict_before,
            "verdict_after": row.verdict_after, "attempts": row.attempts, "skill_path": row.skill_path,
            "skill": row.skill_markdown, "generated_by": row.generated_by}
