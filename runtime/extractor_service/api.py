"""FastAPI service: extraction (sync, webhook or polled), review, metrics, audit.

    POST /extract                         run an extraction (see below)
    GET  /extractions/{job_id}            poll a job: inprogress, or extracted with its result
    GET  /extractions                     recent jobs (?status=inprogress|extracted&client=&flagged=)
    POST /extractions/{job_id}/redeliver  send the result to the job's webhook again
    GET  /review                          flagged results waiting for a person (?status=&client=)
    GET  /review/{job_id}                 one entry, with the result and its flags
    POST /review/{job_id}/resolve         correct or reject; a correction is delivered as human_corrected
    GET  /review/corrections/export       corrected results as ground truth for design-time learning
    GET  /metrics, /metrics/prometheus    throughput, flag rate, latency, cost per client and use case
    GET  /audit/verify                    check the audit hash chain
    GET  /configs, /configs/{c}/{u}/{v}, /graph, /health

``POST /extract`` takes ``file_location`` (required), ``client``, ``usecase``,
``version``, ``extended``, ``idempotency_key``, and how to answer:

* nothing more: the extraction runs now and the response is the result;
* ``callback_url``: 202 at once (``inprogress``); the result is POSTed there;
* ``"async": true``: 202 at once; poll ``GET /extractions/{job_id}``.

``X-Extraction-Status`` is ``inprogress`` or ``extracted``, nothing else. A
finished extraction always has a result: the plain data when it is clean, the
extended form (data, flags, confidence, sources, metadata) when it is flagged
or ``extended`` was asked for. ``X-Extraction-Flagged`` says which.

Requests are validated by plain functions (parse_extract_request).
"""
from __future__ import annotations

import hashlib
import json
import logging
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, PlainTextResponse

from . import pipeline
from .config_store import ConfigStore
from .errors import ConfigError, ServiceError
from .gateway import Gateway
from .graph import build_graph
from .jobs import PUBLIC, JobStore, Runner, check_callback_url, checkpointer

log = logging.getLogger("extractor_service")

_JSON_TYPES = {str: "string", bool: "boolean", int: "integer", list: "array", dict: "object"}
EXTRACT_FIELDS: dict[str, tuple[type, str]] = {
    "file_location": (str, "Path to the input file, inside INPUT_ROOT (absolute, or relative to INPUT_ROOT)."),
    "client": (str, "Config client. Default from defaults.json."),
    "usecase": (str, "Config use case. Default from defaults.json."),
    "version": (str, "Config version, or 'latest'. Default from defaults.json."),
    "extended": (bool, "Always return the extended result (data, flags, confidence, sources, metadata)."),
    "callback_url": (str, "Webhook: answer 202 now and POST the result here when it is ready."),
    "async": (bool, "Answer 202 now; poll GET /extractions/{job_id} for the result."),
    "idempotency_key": (str, "Same key within the dedupe window = the same job and result."),
}
EXTRACT_SCHEMA: dict[str, Any] = {
    "type": "object", "required": ["file_location"], "additionalProperties": False,
    "properties": {n: {"type": _JSON_TYPES[t], "description": d} for n, (t, d) in EXTRACT_FIELDS.items()},
    "examples": [{"file_location": "invoice-email.eml"},
                 {"file_location": "invoice.xlsx", "client": "acme", "callback_url": "https://erp.example/hooks/x"},
                 {"file_location": "statement.csv", "async": True, "idempotency_key": "msg-<c8f2@acme>"}],
}
RESOLVE_FIELDS: dict[str, tuple[type, str]] = {
    "reviewer": (str, "Who made the decision (recorded with every change)."),
    "action": (str, "'correct' (default) or 'reject'."),
    "corrections": (list, "[{'record': 0, 'field': 'total_amount', 'value': 99.5}, ...]"),
    "records": (list, "Replace the records wholesale instead."),
    "note": (str, "Free text kept with the review entry."),
}


def _errors(body: Any, spec: dict[str, tuple[type, str]], required: tuple[str, ...]) -> list[dict[str, str]]:
    if not isinstance(body, dict):
        return [{"loc": "", "msg": "the body must be a JSON object"}]
    errors = [{"loc": k, "msg": "unknown field"} for k in body if k not in spec]
    errors += [{"loc": k, "msg": "required"} for k in required if k not in body]
    for name, (t, _) in spec.items():
        v = body.get(name)
        if v is None:
            continue
        if not isinstance(v, t) or (t is bool) != isinstance(v, bool) or (t is str and not v.strip()):
            errors.append({"loc": name, "msg": {str: "must be a non-empty string", bool: "must be a boolean",
                                                int: "must be an integer", list: "must be an array",
                                                dict: "must be an object"}[t]})
    return errors


def parse_extract_request(body: Any) -> dict[str, Any]:
    """Validate an /extract body. Raises ServiceError(422, "invalid_request") listing every problem."""
    errors = _errors(body, EXTRACT_FIELDS, ("file_location",))
    if errors:
        raise ServiceError(422, "invalid_request", "; ".join(f"{e['loc']}: {e['msg']}" for e in errors),
                           {"errors": errors})
    return {k: body.get(k) for k in EXTRACT_FIELDS} | {"extended": bool(body.get("extended", False)),
                                                       "async": bool(body.get("async", False))}


async def _json(request: Request) -> Any:
    try:
        return json.loads(await request.body() or b"null")
    except ValueError as exc:
        raise ServiceError(422, "invalid_json", f"the body is not JSON: {exc}") from exc


def _headers(job: Any) -> dict[str, str]:
    h = {"X-Extraction-Status": PUBLIC[job["status"]], "X-Job-Id": job["id"]}
    if job["status"] == "done":
        h["X-Extraction-Flagged"] = "true" if job["flagged"] else "false"
        h["X-Audit-Id"] = job["audit_id"] or ""
    return h


def _job_view(store: JobStore, job: Any, *, with_result: bool = True) -> dict[str, Any]:
    out: dict[str, Any] = {"job_id": job["id"], "status": PUBLIC[job["status"]],
                           "created_at": job["created_at"], "client": job["client"], "usecase": job["usecase"]}
    if job["status"] == "done":
        out.update({"flagged": bool(job["flagged"]), "flags": json.loads(job["flags"] or "[]"),
                    "audit_id": job["audit_id"], "human_corrected": bool(job["human_corrected"]),
                    "finished_at": job["finished_at"], "dead_lettered": bool(job["dead"])})
        if with_result:
            out["result"] = pipeline.deliverable(json.loads(job["result"]), bool(job["extended"]))
    if job["callback_url"]:
        out["delivery"] = store.deliveries(job["id"])
    return out


def create_app(settings: pipeline.Settings | None = None, model_gateway: Gateway | None = None,
               *, start_workers: bool | None = None, http: Any = None) -> FastAPI:
    """``model_gateway`` replaces gateway.call_model for every model call (tests, other gateways).

    ``start_workers`` (default: settings.workers > 0) runs queue workers and webhook delivery in this
    process while the app is up; ``http`` replaces the webhook HTTP client (tests).
    """
    settings = settings or pipeline.Settings.from_env()
    state_dir = settings.state_dir or Path(tempfile.mkdtemp(prefix="extractor-state-"))
    store = ConfigStore(settings.config_root)
    problems = store.validate_all()
    saver = checkpointer(state_dir)
    graph = build_graph(store, settings, model_gateway, checkpointer=saver)
    jobs = JobStore(state_dir)
    runner = Runner(jobs, graph, settings, checkpointer=saver, http=http)
    run_workers = settings.workers > 0 if start_workers is None else start_workers

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if run_workers:
            runner.start()
        yield
        if run_workers:
            runner.stop()

    app = FastAPI(title="DataExtractor runtime", version=pipeline.ENGINE_VERSION, lifespan=lifespan,
                  description="Extract fields defined by a data dictionary from email, zip, CSV, Excel, PDF "
                              "and DOCX inputs. Answers now, by webhook, or by polling.")
    app.state.settings, app.state.store, app.state.jobs, app.state.runner = settings, store, jobs, runner

    @app.exception_handler(ServiceError)
    async def _service_error(_: Request, exc: ServiceError) -> JSONResponse:
        return JSONResponse(exc.to_dict(), status_code=exc.status)

    @app.exception_handler(Exception)
    async def _unexpected(_: Request, exc: Exception) -> JSONResponse:
        log.exception("unexpected error")
        return JSONResponse({"error": "internal_error", "message": type(exc).__name__, "detail": {}},
                            status_code=500)

    def _idem_key(req: dict[str, Any]) -> str:
        if req.get("idempotency_key"):
            return "key:" + req["idempotency_key"]
        try:
            data = pipeline.resolve_location(settings, req["file_location"]).read_bytes()
            file_sha = hashlib.sha256(data).hexdigest()
        except ServiceError:
            file_sha = "missing:" + req["file_location"]
        try:
            cfg_sha = store.resolve(req.get("client"), req.get("usecase"), req.get("version")).sha256
        except (ConfigError, ServiceError):
            cfg_sha = "unresolved"
        parts = [file_sha, cfg_sha, req.get("client"), req.get("usecase"), req.get("version"),
                 settings.model_provider]
        return "auto:" + hashlib.sha256(json.dumps(parts).encode()).hexdigest()

    @app.post("/extract", openapi_extra={"requestBody": {
        "required": True, "content": {"application/json": {"schema": EXTRACT_SCHEMA}}}})
    async def extract(request: Request) -> JSONResponse:
        req = parse_extract_request(await _json(request))
        if req.get("callback_url"):
            check_callback_url(req["callback_url"], settings.webhook_allowed_hosts)
        key = await run_in_threadpool(_idem_key, req)
        existing = jobs.find_recent(key, settings.dedupe_window_days)
        if existing is not None:                        # RT-04: the same job, and its audit id
            body = _job_view(jobs, existing)
            code = 200 if existing["status"] == "done" else 202
            if code == 200 and not (req["async"] or req.get("callback_url")):
                return JSONResponse(body["result"], status_code=200, headers=_headers(existing))
            return JSONResponse(body, status_code=code, headers=_headers(existing))
        stored = {k: req[k] for k in ("file_location", "client", "usecase", "version", "idempotency_key")}
        if req["async"] or req.get("callback_url"):
            job_id = jobs.create(key, stored, callback_url=req.get("callback_url"), extended=req["extended"])
            job = jobs.get(job_id)
            return JSONResponse({"job_id": job_id, "status": "inprogress", "poll": f"/extractions/{job_id}"},
                                status_code=202, headers={**_headers(job), "Location": f"/extractions/{job_id}"})
        job_id = jobs.create(key, stored, callback_url=None, extended=req["extended"], running_by="sync",
                             lease_s=settings.job_lease_s)
        await run_in_threadpool(runner.execute, jobs.get(job_id))
        job = jobs.get(job_id)
        if job["status"] != "done":                     # an engine fault queued it for retry
            return JSONResponse(_job_view(jobs, job), status_code=202, headers=_headers(job))
        return JSONResponse(pipeline.deliverable(json.loads(job["result"]), req["extended"]),
                            headers=_headers(job))

    @app.get("/extractions/{job_id}")
    def get_extraction(job_id: str) -> JSONResponse:
        job = jobs.get(job_id)
        if job is None:
            raise ServiceError(404, "job_not_found", f"no job {job_id}")
        return JSONResponse(_job_view(jobs, job), headers=_headers(job))

    @app.get("/extractions")
    def list_extractions(status: str | None = None, client: str | None = None, flagged: bool | None = None,
                         limit: int = 50) -> list[dict[str, Any]]:
        return [_job_view(jobs, j, with_result=False) for j in jobs.list(status=status, client=client,
                                                                          flagged=flagged, limit=limit)]

    @app.post("/extractions/{job_id}/redeliver", status_code=202)
    def redeliver(job_id: str) -> dict[str, Any]:
        job = jobs.get(job_id)
        if job is None:
            raise ServiceError(404, "job_not_found", f"no job {job_id}")
        if not job["callback_url"]:
            raise ServiceError(409, "no_callback", "this job has no callback_url")
        if job["status"] != "done":
            raise ServiceError(409, "job_inprogress", "the job has no result yet")
        jobs.enqueue_delivery(job_id, "extraction.corrected" if job["human_corrected"] else "extraction.completed")
        return {"job_id": job_id, "delivery": "queued"}

    # ------------------------------------------------------------------ review

    @app.get("/review")
    def review(status: str | None = "open", client: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        return [{"job_id": r["job_id"], "audit_id": r["audit_id"], "client": r["client"], "usecase": r["usecase"],
                 "flags": json.loads(r["flags"] or "[]"), "status": r["status"], "opened_at": r["opened_at"],
                 "resolved_at": r["resolved_at"], "reviewer": r["reviewer"]}
                for r in jobs.review_list(status=status or None, client=client, limit=limit)]

    @app.get("/review/corrections/export")
    def corrections_export(client: str | None = None, usecase: str | None = None) -> dict[str, Any]:
        return {"items": jobs.corrections_export(client, usecase)}

    @app.get("/review/{job_id}")
    def review_entry(job_id: str) -> dict[str, Any]:
        entry, job = jobs.review_get(job_id), jobs.get(job_id)
        if entry is None or job is None:
            raise ServiceError(404, "review_not_found", f"no review entry for {job_id}")
        return {"job_id": job_id, "status": entry["status"], "flags": json.loads(entry["flags"] or "[]"),
                "reviewer": entry["reviewer"], "note": entry["note"],
                "result": json.loads(job["result"]),
                "original_result": json.loads(job["original_result"]) if job["original_result"] else None}

    @app.post("/review/{job_id}/resolve", openapi_extra={"requestBody": {"required": True, "content": {
        "application/json": {"schema": {"type": "object", "required": ["reviewer"], "additionalProperties": False,
                                        "properties": {n: {"type": _JSON_TYPES[t], "description": d}
                                                       for n, (t, d) in RESOLVE_FIELDS.items()}}}}}})
    async def resolve(job_id: str, request: Request) -> dict[str, Any]:
        body = await _json(request)
        errors = _errors(body, RESOLVE_FIELDS, ("reviewer",))
        if not errors and body.get("action", "correct") not in ("correct", "reject"):
            errors.append({"loc": "action", "msg": "must be 'correct' or 'reject'"})
        for i, c in enumerate(body.get("corrections") or [] if isinstance(body, dict) else []):
            if not isinstance(c, dict) or not isinstance(c.get("record"), int) or not isinstance(c.get("field"), str):
                errors.append({"loc": f"corrections[{i}]", "msg": "needs record (integer), field and value"})
        if errors:
            raise ServiceError(422, "invalid_request", "; ".join(f"{e['loc']}: {e['msg']}" for e in errors),
                               {"errors": errors})
        return await run_in_threadpool(jobs.resolve, job_id, reviewer=body["reviewer"],
                                       action=body.get("action", "correct"), corrections=body.get("corrections") or [],
                                       records=body.get("records"), note=body.get("note"))

    # ------------------------------------------------------------------ operations

    @app.get("/metrics")
    def metrics(client: str | None = None) -> dict[str, Any]:
        return jobs.metrics(client)

    @app.get("/metrics/prometheus", response_class=PlainTextResponse)
    def metrics_prometheus() -> str:
        lines = []
        for g in jobs.metrics()["groups"]:
            lab = f'client="{g["client"]}",usecase="{g["usecase"]}"'
            lines += [f"extractor_jobs_total{{{lab}}} {g['jobs']}",
                      f"extractor_jobs_inprogress{{{lab}}} {g['inprogress']}",
                      f"extractor_jobs_flagged_total{{{lab}}} {g['flagged']}",
                      f"extractor_jobs_corrected_total{{{lab}}} {g['corrected']}",
                      f"extractor_jobs_dead_lettered_total{{{lab}}} {g['dead_lettered']}"]
            for q in ("p50", "p95"):
                if g["latency_ms"][q] is not None:
                    lines.append(f'extractor_latency_ms{{{lab},quantile="{q[1:]}"}} {g["latency_ms"][q]}')
            if g["cost_usd"]["total"] is not None:
                lines.append(f"extractor_cost_usd_total{{{lab}}} {g['cost_usd']['total']}")
        return "\n".join(lines) + "\n"

    @app.get("/audit/verify")
    def audit_verify() -> dict[str, Any]:
        return jobs.verify_chain()

    @app.get("/configs")
    def configs() -> dict[str, Any]:
        return {"defaults": store.defaults(), "configs": store.catalogue()}

    @app.get("/configs/{client}/{usecase}/{version}")
    def config(client: str, usecase: str, version: str) -> dict[str, Any]:
        cfg = store.resolve(client, usecase, version)
        return {**cfg.ref(), "manifest": cfg.manifest, "data_dictionary": cfg.dictionary.describe(),
                "skills": [{"name": s.name, "applies_to": s.applies_to or None} for s in cfg.skills],
                "ingestion_filter": {"active": cfg.ingestion_filter.active}}

    @app.get("/graph", response_class=PlainTextResponse)
    def graph_mermaid() -> str:
        return graph.get_graph().draw_mermaid()

    @app.get("/health")
    def health() -> dict[str, Any]:
        from . import ocr
        return {"status": "ok" if not problems else "degraded", "engine_version": pipeline.ENGINE_VERSION,
                "config_root": str(settings.config_root), "input_root": str(settings.input_root),
                "state_dir": str(state_dir), "workers": settings.workers if run_workers else 0,
                "parse_sandbox": settings.parse_sandbox, "ocr_available": ocr.available(),
                "model_provider_override": settings.model_provider, "config_problems": problems}

    return app


def _lazy_app() -> FastAPI:
    return create_app()


class _App:
    """``uvicorn extractor_service.api:app`` builds the app on first use, not on import."""

    _app: FastAPI | None = None

    async def __call__(self, scope, receive, send):
        if _App._app is None:
            _App._app = _lazy_app()
        await _App._app(scope, receive, send)


app = _App()
