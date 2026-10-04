"""The authoring run behind one route: a corpus becomes a candidate config version."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ...agents.base import AgentError
from ...model.base import ModelClient
from ...orchestrator import AuthoringRunInput, AuthoringRunOutput, run_authoring
from ...registry.errors import RegistryError
from ..deps import as_http, model_dep, session_dep
from ..typed import docs, json_body, out

router = APIRouter(prefix="/runs", tags=["runs"])


@router.post(
    "",
    summary="Author a use case from a corpus: build, evaluate in the runtime, publish a candidate (DT-01)",
    **docs(AuthoringRunInput, AuthoringRunOutput),
)
def authoring_run(
    payload: AuthoringRunInput = Depends(json_body(AuthoringRunInput)),
    model: ModelClient = Depends(model_dep),
    session: Session = Depends(session_dep),
):
    try:
        run = run_authoring(payload, model=model, session=session)
    except (AgentError, RegistryError) as exc:
        raise as_http(exc) from exc
    return out(run)
