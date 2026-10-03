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

import json
import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, PlainTextResponse

from . import pipeline
from .config_store import ConfigStore
from .errors import ModelError, ServiceError
from .gateway import Gateway
from .graph import build_graph


log = logging.getLogger("extractor_service")


EXTRACT_FIELDS: dict[str, tuple[type, str]] = {
    "file_location": (str, "Path to the input file, inside INPUT_ROOT (absolute, or relative to INPUT_ROOT)."),
    "client": (str, "Config client. Default from defaults.json."),
    "usecase": (str, "Config use case. Default from defaults.json."),
    "version": (str, "Config version, or 'latest'. Default from defaults.json."),
    "extended": (bool, "Return confidence, sources and metadata with the data."),
}
_JSON_TYPES = {str: "string", bool: "boolean"}

#: The /extract request body, published in OpenAPI.
EXTRACT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["file_location"],
    "additionalProperties": False,
    "properties": {name: {"type": _JSON_TYPES[t], "description": d} for name, (t, d) in EXTRACT_FIELDS.items()},
    "examples": [
        {"file_location": "invoice-email.eml"},
        {"file_location": "invoice.xlsx", "client": "acme", "usecase": "ap-invoices",
         "version": "1.1.0", "extended": True},
    ],
}


def parse_extract_request(body: Any) -> dict[str, Any]:
    """Validate an /extract body. Raises ServiceError(422, "invalid_request") listing every problem."""
    if not isinstance(body, dict):
        raise ServiceError(422, "invalid_request", "the body must be a JSON object")
    errors = [{"loc": k, "msg": "unknown field"} for k in body if k not in EXTRACT_FIELDS]
    if "file_location" not in body:
        errors.append({"loc": "file_location", "msg": "required"})
    for name, (t, _) in EXTRACT_FIELDS.items():
        v = body.get(name)
        if v is None:
            continue
        if not isinstance(v, t) or (t is str and not v.strip()):
            errors.append({"loc": name, "msg": f"must be a non-empty {_JSON_TYPES[t]}"
                           if t is str else f"must be a {_JSON_TYPES[t]}"})
    if errors:
        raise ServiceError(422, "invalid_request", "; ".join(f"{e['loc']}: {e['msg']}" for e in errors),
                           {"errors": errors})
    return {"file_location": body["file_location"], "client": body.get("client"),
            "usecase": body.get("usecase"), "version": body.get("version"),
            "extended": bool(body.get("extended", False))}


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

    @app.post("/extract", openapi_extra={"requestBody": {
        "required": True, "content": {"application/json": {"schema": EXTRACT_SCHEMA}}}})
    async def extract(request: Request) -> JSONResponse:
        try:
            raw = json.loads(await request.body() or b"null")
        except ValueError as exc:
            raise ServiceError(422, "invalid_json", f"the body is not JSON: {exc}") from exc
        req = parse_extract_request(raw)
        out = await run_in_threadpool(pipeline.run, graph, settings, file_location=req["file_location"],
                                      client=req["client"], usecase=req["usecase"], version=req["version"])
        body = out.extended if req["extended"] else out.data
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
