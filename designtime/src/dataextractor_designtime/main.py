"""FastAPI application.

Every design-time component is mounted as its own route so it can be exercised
alone; ``/runs`` chains them. The OpenAPI document at ``/docs`` is the contract.
"""

from __future__ import annotations

from fastapi import FastAPI
from sqlalchemy import text

from . import __version__
from .agents import AGENTS
from .api.routers import agents as agents_router
from .api.routers import learning as learning_router
from .api.routers import registry as registry_router
from .api.routers import runs as runs_router
from .config import get_settings
from .registry.db import get_engine

app = FastAPI(
    title="DataExtractor design-time",
    version=__version__,
    summary="Design agents, the Postgres artifact registry, and the authoring run.",
    description=(
        "Implements PRD 2 (design-time) against the contracts in PRD 1. "
        "Each agent is reachable at /agents/{name}/run with its own typed "
        "request and response; /runs chains them into one authoring run; "
        "/learning/runs learns a skill from one sample of a document pattern."
    ),
)

app.include_router(agents_router.router)
app.include_router(registry_router.router)
app.include_router(runs_router.router)
app.include_router(learning_router.router)


@app.get("/health", tags=["meta"], summary="Liveness and database reachability")
def health() -> dict[str, object]:
    settings = get_settings()
    db_ok = True
    detail = "ok"
    try:
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:  # reported, not raised: health should answer
        db_ok = False
        detail = str(exc).splitlines()[0][:200]
    return {
        "status": "ok" if db_ok else "degraded",
        "version": __version__,
        "engine_version": settings.engine_version,
        "database": {"reachable": db_ok, "detail": detail},
    }


@app.get("/components", tags=["meta"], summary="The components exposed for individual testing")
def components() -> dict[str, object]:
    return {
        "agents": [
            {"name": name, "identity": cls().identity, "endpoint": f"/agents/{name}/run"}
            for name, cls in AGENTS.items()
        ],
        "registry": "/registry",
        "authoring_run": "/runs",
        "pattern_learning": "/learning/runs",
    }
