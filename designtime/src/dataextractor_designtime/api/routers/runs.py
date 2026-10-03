"""The whole authoring loop behind one route, plus optional publication."""

from __future__ import annotations

from dataclasses import dataclass, fields

from fastapi import APIRouter, Depends

from ...records import Record
from ...agents.base import AgentError
from ...contracts.artifacts import PackageManifest
from ...model.base import ModelClient
from ...orchestrator import AuthoringRunInput, AuthoringRunOutput, run_authoring
from ...registry.errors import RegistryError
from ...registry.repository import Registry
from ..deps import as_http, model_dep, registry_dep
from ..typed import docs, json_body, out

router = APIRouter(prefix="/runs", tags=["runs"])


@dataclass(kw_only=True)
class AuthoringRunRequest(AuthoringRunInput):
    #: Publish the resulting package to the UAT channel when the run finishes.
    publish: bool = False


@dataclass(kw_only=True)
class AuthoringRunResponse(Record):
    run: AuthoringRunOutput
    published: bool = False
    package_state: str | None = None


@router.post(
    "",
    summary="Run the full design-time loop (DT-01)",
    **docs(AuthoringRunRequest, AuthoringRunResponse),
)
def authoring_run(
    payload: AuthoringRunRequest = Depends(json_body(AuthoringRunRequest)),
    model: ModelClient = Depends(model_dep),
    registry: Registry = Depends(registry_dep),
):
    body = AuthoringRunInput(**{f.name: getattr(payload, f.name) for f in fields(AuthoringRunInput)})
    try:
        run = run_authoring(body, model=model)
    except AgentError as exc:
        raise as_http(exc) from exc

    if not payload.publish:
        return out(AuthoringRunResponse(run=run))

    try:
        pkg = registry.publish(
            manifest=PackageManifest.from_dict(run.manifest),
            artifacts=run.artifacts,
            bodies=run.bodies,
            eval_report=run.eval_report,
            gate_failed=run.gate_failed,
        )
    except RegistryError as exc:
        raise as_http(exc) from exc
    return out(AuthoringRunResponse(run=run, published=True, package_state=pkg.state.value))
