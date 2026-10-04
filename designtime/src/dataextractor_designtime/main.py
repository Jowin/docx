"""FastAPI application.

Every design-time component is mounted as its own route so it can be exercised
alone; ``/runs`` chains them. The OpenAPI document at ``/docs`` is the contract.
"""

from __future__ import annotations

import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse
from sqlalchemy import text

from . import __version__
from .agents import AGENTS
from .api.routers import agents as agents_router
from .api.routers import configs as configs_router
from .api.routers import learning as learning_router
from .api.routers import runs as runs_router
from .config import get_settings
from .registry.db import get_engine

app = FastAPI(
    title="DataExtractor design-time",
    version=__version__,
    summary="Design agents, the authoring run, pattern learning, and config versions.",
    description=(
        "Implements PRD 2 (design-time) against the contracts in PRD 1. "
        "The runtime's config folders are the source of truth: /runs (a corpus) and "
        "/learning/runs (one sample) both publish candidate versions into them, scored by "
        "the real runtime; /configs signs off, releases and rolls back. Each agent is "
        "reachable at /agents/{name}/run with its own typed request and response."
    ),
)

_origins = [o.strip() for o in os.getenv("CORS_ORIGINS", "").split(",") if o.strip()]
if _origins:
    app.add_middleware(CORSMiddleware, allow_origins=_origins, allow_methods=["*"], allow_headers=["*"])

app.include_router(agents_router.router)
app.include_router(configs_router.router)
app.include_router(runs_router.router)
app.include_router(learning_router.router)


@app.get("/graphs/{name}", tags=["meta"], response_class=PlainTextResponse,
         summary="A design graph as Mermaid: authoring or learning")
def graph(name: str) -> str:
    from fastapi import HTTPException
    if name == "authoring":
        from .orchestrator import build_authoring_graph
        return build_authoring_graph().get_graph().draw_mermaid()
    if name == "learning":
        from .learning.graph import build_learning_graph
        return build_learning_graph(runtime=None, store=None, model=None, config_root=get_settings()
                                    .runtime_config_root).get_graph().draw_mermaid()
    raise HTTPException(404, {"code": "graph_not_found", "message": "authoring or learning"})


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
        "config_versions": "/configs",
        "ingestion_lookups": "/lookups",
        "authoring_run": "/runs",
        "pattern_learning": "/learning/runs",
        "graphs": ["/graphs/authoring", "/graphs/learning"],
    }
