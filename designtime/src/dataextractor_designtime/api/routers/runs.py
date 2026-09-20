"""The whole authoring loop behind one route, plus optional publication."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict

from ...agents.base import AgentError
from ...contracts.artifacts import PackageManifest
from ...model.base import ModelClient
from ...orchestrator import AuthoringRunInput, AuthoringRunOutput, run_authoring
from ...registry.errors import RegistryError
from ...registry.repository import Registry
from ..deps import as_http, model_dep, registry_dep

router = APIRouter(prefix="/runs", tags=["runs"])


class AuthoringRunRequest(AuthoringRunInput):
    model_config = ConfigDict(extra="forbid")

    #: Publish the resulting package to the UAT channel when the run finishes.
    publish: bool = False


class AuthoringRunResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run: AuthoringRunOutput
    published: bool = False
    package_state: str | None = None


@router.post(
    "",
    response_model=AuthoringRunResponse,
    summary="Run the full design-time loop (DT-01)",
)
def authoring_run(
    payload: AuthoringRunRequest,
    model: ModelClient = Depends(model_dep),
    registry: Registry = Depends(registry_dep),
):
    body = AuthoringRunInput(**payload.model_dump(exclude={"publish"}))
    try:
        out = run_authoring(body, model=model)
    except AgentError as exc:
        raise as_http(exc) from exc

    if not payload.publish:
        return AuthoringRunResponse(run=out)

    try:
        pkg = registry.publish(
            manifest=PackageManifest(**out.manifest),
            artifacts=out.artifacts,
            bodies=out.bodies,
            eval_report=out.eval_report,
            gate_failed=out.gate_failed,
        )
    except RegistryError as exc:
        raise as_http(exc) from exc
    return AuthoringRunResponse(run=out, published=True, package_state=pkg.state.value)
