"""Pattern learning: learn from a sample, resume a cut-off call, learn from corrections, read the record."""

from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, Depends, Query, Request

from ...agents.base import AgentError
from ...config import Settings
from ...learning import IsolatedRuntime, LearnRequest, LearnResponse, LearnRunSummary, LearningStore, learn
from ...learning.graph import resume as resume_learning
from ...learning.memory import open_memory
from ...learning.models import CorrectionResult, CorrectionsRequest, CorrectionsResponse
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


def _db_url(request: Request, settings: Settings) -> str:
    return getattr(request.app.state, "database_url", None) or settings.database_url


def _summary(row) -> LearnRunSummary:
    return LearnRunSummary(id=row.id, kind=row.kind or "pattern", created_at=row.created_at.isoformat(), client=row.client_id,
                           usecase=row.usecase, object=row.object_name, pattern_name=row.pattern_name,
                           source=row.source, outcome=row.outcome, base_version=row.base_version,
                           result_version=row.result_version, passed_before=row.passed_before,
                           passed_after=row.passed_after, requested_by=row.requested_by)


def _common(settings: Settings) -> dict[str, Any]:
    return {"config_root": settings.runtime_config_root, "regression_limit": settings.learning_regression_samples,
            "state_root": settings.learning_state_dir}


@router.post(
    "/runs",
    summary="Extract a sample in the isolated runtime; when it fails, learn a skill for its pattern",
    **docs(LearnRequest, LearnResponse),
)
def learn_from_sample(
    request: Request,
    payload: LearnRequest = Depends(json_body(LearnRequest)),
    runtime: IsolatedRuntime = Depends(runtime_dep),
    store: LearningStore = Depends(store_dep),
    model: ModelClient = Depends(model_dep),
    settings: Settings = Depends(settings_dep),
):
    try:
        with open_memory(_db_url(request, settings)) as (memory, saver):
            result = learn(payload, runtime=runtime, store=store, model=model, memory=memory, checkpointer=saver,
                           max_iterations=settings.learning_max_iterations, **_common(settings))
    except (AgentError, ValidationError) as exc:
        raise as_http(exc) from exc
    return out(result)


@router.post(
    "/runs/{run_id}/resume",
    summary="Carry an interrupted learning call on from its last checkpoint",
    **docs(response=LearnResponse),
)
def resume_run(
    run_id: str,
    request: Request,
    runtime: IsolatedRuntime = Depends(runtime_dep),
    store: LearningStore = Depends(store_dep),
    model: ModelClient = Depends(model_dep),
    settings: Settings = Depends(settings_dep),
):
    try:
        with open_memory(_db_url(request, settings)) as (memory, saver):
            result = resume_learning(run_id, runtime=runtime, store=store, model=model, memory=memory,
                                     checkpointer=saver, **_common(settings))
    except (AgentError, ValidationError) as exc:
        raise as_http(exc) from exc
    return out(result)


def _pattern_for(item) -> str:
    m = re.search(r"@([A-Za-z0-9.-]+)", item.sender or "")
    base = m.group(1) if m else item.file_location.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    slug = re.sub(r"[^a-z0-9]+", "-", base.lower()).strip("-")[:48] or "corrections"
    return f"corrected-{slug}"


@router.post(
    "/corrections",
    summary="Learn from reviewer corrections: each corrected result is a sample with ground truth",
    **docs(CorrectionsRequest, CorrectionsResponse),
)
def learn_from_corrections(
    request: Request,
    payload: CorrectionsRequest = Depends(json_body(CorrectionsRequest)),
    runtime: IsolatedRuntime = Depends(runtime_dep),
    store: LearningStore = Depends(store_dep),
    model: ModelClient = Depends(model_dep),
    settings: Settings = Depends(settings_dep),
):
    items = list(payload.items)
    if not items:
        base = payload.runtime_url or settings.runtime_url
        if not base:
            raise as_http(AgentError("give items, or runtime_url (or set RUNTIME_URL) to fetch them",
                                     code="invalid_input"))
        import httpx
        from ...learning.models import CorrectionItem
        try:
            params = {k: v for k, v in (("client", payload.client), ("usecase", payload.usecase)) if v}
            resp = httpx.get(f"{base.rstrip('/')}/review/corrections/export", params=params, timeout=30)
            resp.raise_for_status()
            items = [CorrectionItem.from_dict(i) for i in resp.json().get("items", [])]
        except (httpx.HTTPError, ValueError) as exc:
            raise as_http(AgentError(f"cannot fetch corrections from the runtime: {exc}",
                                     code="runtime_unreachable", status=502)) from exc
    results: list[CorrectionResult] = []
    with open_memory(_db_url(request, settings)) as (memory, saver):
        for item in items:
            client, usecase = item.client or payload.client, item.usecase or payload.usecase
            seen = memory.correction_seen(client or "-", usecase or "-", item.job_id)
            if seen:
                results.append(CorrectionResult(job_id=item.job_id, status="already_imported",
                                                pattern_name=seen.get("pattern_name"),
                                                learning_run=seen.get("learning_run")))
                continue
            pattern = payload.pattern_name or _pattern_for(item)
            req = LearnRequest(source=item.file_location, pattern_name=pattern, client=client, usecase=usecase,
                               ground_truth=item.ground_truth, publish=payload.publish,
                               max_iterations=payload.max_iterations, scope=payload.scope,
                               requested_by=f"review:{item.reviewer or 'unknown'}")
            try:
                res = learn(req, runtime=runtime, store=store, model=model, memory=memory, checkpointer=saver,
                            max_iterations=settings.learning_max_iterations, **_common(settings))
            except AgentError as exc:
                results.append(CorrectionResult(job_id=item.job_id, status="error", pattern_name=pattern,
                                                error=f"{exc.code}: {exc}"))
                continue
            memory.remember_correction(res.client, res.usecase, item.job_id,
                                       {"pattern_name": pattern, "learning_run": res.id, "outcome": res.outcome,
                                        "audit_id": item.audit_id, "reviewer": item.reviewer})
            results.append(CorrectionResult(job_id=item.job_id, status="learned_from", pattern_name=pattern,
                                            learning_run=res.id, outcome=res.outcome,
                                            result_version=res.result_version))
    return out(CorrectionsResponse(results=results))


@router.get("/memory", summary="What learning remembers for a client and use case")
def memory_view(request: Request, client: str = Query(...), usecase: str = Query(...),
                settings: Settings = Depends(settings_dep)) -> dict[str, Any]:
    with open_memory(_db_url(request, settings)) as (memory, _):
        return {"client": client, "usecase": usecase, "rejected_hints": memory.rejected(client, usecase),
                "patterns": memory.patterns(client, usecase), "corrections": memory.corrections(client, usecase)}


@router.delete("/memory/rejected-hints", summary="Forget one rejected hint so the writer may propose it again")
def forget_hint(request: Request, client: str = Query(...), usecase: str = Query(...), key: str = Query(...),
                settings: Settings = Depends(settings_dep)) -> dict[str, Any]:
    with open_memory(_db_url(request, settings)) as (memory, _):
        memory.forget_rejected(client, usecase, key)
    return {"forgotten": key}


@router.get("/runs", summary="Design runs, newest first: pattern learning and authoring (kind=)",
            **docs(response=LearnRunSummary, many=True))
def list_runs(
    kind: str | None = Query(default=None, pattern="^(pattern|authoring)$"),
    client: str | None = Query(default=None),
    usecase: str | None = Query(default=None),
    pattern_name: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    store: LearningStore = Depends(store_dep),
):
    return out([_summary(r) for r in store.list(client, usecase, pattern_name, limit, kind=kind)])


@router.get("/runs/{run_id}", summary="One learning run as recorded, attempts and skill included")
def get_run(run_id: str, store: LearningStore = Depends(store_dep)) -> Any:
    try:
        row = store.get(run_id)
    except AgentError as exc:
        raise as_http(exc) from exc
    return {**out(_summary(row)), "source_sha256": row.source_sha256, "ground_truth": row.ground_truth,
            "reference_text": row.reference_text, "verdict_before": row.verdict_before,
            "verdict_after": row.verdict_after, "attempts": row.attempts, "skill_path": row.skill_path,
            "skill": row.skill_markdown, "generated_by": row.generated_by, "error": row.error,
            "path": row.path or []}
