"""FastAPI service for local testing and for the deployed image.

    POST /extract     {"file_location": "...", "client"?, "usecase"?, "version"?, "extended"?}
    GET  /configs     every client / use case / version on disk, plus the defaults
    GET  /configs/{client}/{usecase}/{version}   one config's manifest and data dictionary
    GET  /graph       the pipeline's LangGraph graph as Mermaid text
    GET  /health      liveness and config problems found at startup

Plain output is the data alone. ``"extended": true`` adds confidence, sources,
review reasons and metadata. Both carry the run's status and audit id in the
``X-Extraction-Status`` and ``X-Audit-Id`` response headers.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field

from . import pipeline
from .config_store import ConfigStore
from .errors import ModelError, ServiceError
from .gateway import Gateway
from .graph import build_graph


log = logging.getLogger("extractor_service")


class ExtractRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", json_schema_extra={"examples": [
        {"file_location": "invoice-email.eml"},
        {"file_location": "invoice.xlsx", "client": "acme", "usecase": "ap-invoices",
         "version": "1.1.0", "extended": True}]})

    file_location: str = Field(..., description="Path to the input file, inside INPUT_ROOT "
                                                "(absolute, or relative to INPUT_ROOT).")
    client: str | None = Field(None, description="Config client. Default from defaults.json.")
    usecase: str | None = Field(None, description="Config use case. Default from defaults.json.")
    version: str | None = Field(None, description="Config version, or 'latest'. Default from defaults.json.")
    extended: bool = Field(False, description="Return confidence, sources and metadata with the data.")


def create_app(settings: pipeline.Settings | None = None,
               model_gateway: Gateway | None = None) -> FastAPI:
    """``model_gateway`` replaces gateway.call_model for every model call (tests, other gateways)."""
    settings = settings or pipeline.Settings.from_env()
    store = ConfigStore(settings.config_root)
    problems = store.validate_all()
    graph = build_graph(store, settings, model_gateway)
    app = FastAPI(title="DataExtractor runtime", version=pipeline.ENGINE_VERSION,
                  description="Extract fields defined by a data dictionary from CSV, Excel, PDF, "
                              "email and zip inputs.")
    app.state.settings, app.state.store = settings, store

    @app.exception_handler(ServiceError)
    async def _service_error(_: Request, exc: ServiceError) -> JSONResponse:
        return JSONResponse(exc.to_dict(), status_code=exc.status)

    @app.exception_handler(ModelError)
    async def _model_error(_: Request, exc: ModelError) -> JSONResponse:
        status = {"model_unavailable": 503, "model_timeout": 504}.get(exc.code, 502)
        return JSONResponse({"error": exc.code, "message": str(exc),
                             "detail": {"transient": exc.transient}}, status_code=status)

    @app.exception_handler(Exception)
    async def _unexpected(_: Request, exc: Exception) -> JSONResponse:
        log.exception("unexpected error")
        return JSONResponse({"error": "internal_error", "message": type(exc).__name__,
                             "detail": {}}, status_code=500)

    @app.post("/extract")
    def extract(req: ExtractRequest) -> JSONResponse:
        out = pipeline.run(graph, settings, file_location=req.file_location, client=req.client,
                           usecase=req.usecase, version=req.version)
        body = out.extended if req.extended else out.data
        return JSONResponse(body, headers={"X-Extraction-Status": out.status,
                                           "X-Audit-Id": out.audit_id})

    @app.get("/configs")
    def configs() -> dict[str, Any]:
        return {"defaults": store.defaults(), "configs": store.catalogue()}

    @app.get("/configs/{client}/{usecase}/{version}")
    def config(client: str, usecase: str, version: str) -> dict[str, Any]:
        cfg = store.resolve(client, usecase, version)
        return {**cfg.ref(), "manifest": cfg.manifest, "data_dictionary": cfg.dictionary.describe(),
                "skills": [s.name for s in cfg.skills]}

    @app.get("/graph", response_class=PlainTextResponse)
    def graph_mermaid() -> str:
        return graph.get_graph().draw_mermaid()

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {"status": "ok" if not problems else "degraded", "engine_version": pipeline.ENGINE_VERSION,
                "config_root": str(settings.config_root), "input_root": str(settings.input_root),
                "model_provider_override": settings.model_provider, "config_problems": problems}

    return app


app = create_app()
