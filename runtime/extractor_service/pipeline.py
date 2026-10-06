"""Run one extraction: settings, file-location checks, the graph call, and the result.

The steps live in graph.py (a LangGraph state graph); verification and scoring
in verify.py; every model call goes through gateway.py.

**The result contract.** Every run ends in a result, whatever happened:

* status is always ``extracted`` once the run is finished (``inprogress`` only
  while a job waits or runs, see jobs.py);
* a clean run (no flags) delivers the plain data: an array of records;
* a flagged run (low confidence, a missing or unverified field, a skipped
  attachment, a model failure, a file that could not be read, ...) delivers the
  extended form: the data plus ``flagged``, ``flags``, confidence, per-field
  sources and metadata, exactly what ``"extended": true`` returns;
* a run that failed outright (no such file, unknown config, unsupported input)
  is a flagged result with no records and ``flags: ["error:<code>"]``.

``deliverable(extended, want_extended)`` picks between the two forms.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config_store import ConfigStore
from .errors import ModelError, ServiceError
from .schema import to_json
from .verify import field_out

ENGINE_VERSION = "0.5.0"


def _default_sandbox() -> str:
    return "process" if os.name == "posix" else "off"


@dataclass(frozen=True)
class Settings:
    config_root: Path
    input_root: Path
    audit_dir: Path | None = None
    model_provider: str | None = None      # override every config's provider, e.g. "stub"
    max_file_mb: int = 25
    #: Postgres for jobs, deliveries, review, the audit chain, the spool and checkpoints.
    database_url: str | None = None
    #: The Postgres schema those tables live in.
    db_schema: str = "extractor"
    #: Fernet key: encrypts spooled attachment bytes at rest.
    state_key: str | None = None
    parse_sandbox: str = field(default_factory=_default_sandbox)
    sandbox_memory_mb: int = 1024
    workers: int = 2
    job_max_attempts: int = 3
    job_retry_backoff_s: float = 2.0     # an engine fault waits backoff * 2^(attempt-1), at most 60 s
    job_lease_s: float = 420.0
    dedupe_window_days: float = 7.0
    webhook_secret: str | None = None
    webhook_max_attempts: int = 8
    webhook_timeout_s: float = 15.0
    webhook_backoff_s: float = 5.0
    webhook_allowed_hosts: tuple[str, ...] = ()
    retention_days: float = 90.0
    poll_interval_s: float = 0.5
    #: Where each finished job's result is written as files; None = write none (the CLI, tests).
    output_root: Path | None = None
    #: Formats written when neither the request nor the config says otherwise.
    output_formats: tuple[str, ...] = ("csv",)

    @classmethod
    def from_env(cls) -> "Settings":
        here = Path(__file__).resolve().parent.parent
        env = os.environ.get
        audit = env("AUDIT_DIR")
        hosts = tuple(h.strip().lower() for h in (env("WEBHOOK_ALLOWED_HOSTS") or "").split(",") if h.strip())
        return cls(config_root=Path(env("CONFIG_ROOT", str(here / "configs"))),
                   input_root=Path(env("INPUT_ROOT", str(here / "data"))),
                   audit_dir=Path(audit) if audit else None,
                   model_provider=env("MODEL_PROVIDER") or None,
                   max_file_mb=int(env("MAX_FILE_MB", "25")),
                   database_url=env("DATABASE_URL") or None,
                   db_schema=env("DB_SCHEMA") or "extractor",
                   state_key=env("STATE_KEY") or None,
                   parse_sandbox=env("PARSE_SANDBOX") or _default_sandbox(),
                   sandbox_memory_mb=int(env("SANDBOX_MEMORY_MB", "1024")),
                   workers=int(env("WORKERS", "2")),
                   job_max_attempts=int(env("JOB_MAX_ATTEMPTS", "3")),
                   job_retry_backoff_s=float(env("JOB_RETRY_BACKOFF_S", "2")),
                   job_lease_s=float(env("JOB_LEASE_S", "420")),
                   dedupe_window_days=float(env("DEDUPE_WINDOW_DAYS", "7")),
                   webhook_secret=env("WEBHOOK_SECRET") or None,
                   webhook_max_attempts=int(env("WEBHOOK_MAX_ATTEMPTS", "8")),
                   webhook_timeout_s=float(env("WEBHOOK_TIMEOUT_S", "15")),
                   webhook_backoff_s=float(env("WEBHOOK_BACKOFF_S", "5")),
                   webhook_allowed_hosts=hosts,
                   retention_days=float(env("RETENTION_DAYS", "90")),
                   output_root=Path(env("OUTPUT_ROOT", str(here / "output"))) if env("OUTPUT_ROOT", "x") else None,
                   output_formats=_formats(env("OUTPUT_FORMATS", "csv")))


def _formats(text: str) -> tuple[str, ...]:
    return tuple(f.strip().lower() for f in text.split(",") if f.strip() and f.strip().lower() != "none")


@dataclass
class Outcome:
    data: list[dict[str, Any]]
    extended: dict[str, Any]
    flagged: bool
    audit_id: str
    flags: list[str] = field(default_factory=list)
    cfg: Any = field(default=None, repr=False)            # the ExtractionConfig used (None on early failure)
    docs: list[Any] = field(default_factory=list, repr=False)  # evidence documents read

    @property
    def status(self) -> str:
        return "extracted"


def deliverable(extended: dict[str, Any], want_extended: bool) -> Any:
    """What a caller receives: the plain data when the run is clean, the extended form when flagged."""
    return extended if (want_extended or extended.get("flagged")) else extended["data"]


# ------------------------------------------------------------------ file location

def resolve_location(settings: Settings, location: str) -> Path:
    """The file must sit inside INPUT_ROOT; relative paths are taken from there."""
    root = settings.input_root.resolve()
    raw = Path(location)
    path = (raw if raw.is_absolute() else root / raw).resolve()
    if path != root and root not in path.parents:
        raise ServiceError(400, "location_outside_input_root",
                           f"file_location must be inside {root}", {"file_location": location})
    if not path.is_file():
        raise ServiceError(404, "file_not_found", f"no file at {location}", {"file_location": location})
    size = path.stat().st_size
    if size > settings.max_file_mb * 1024 * 1024:
        raise ServiceError(413, "input_too_large", f"file is {size} bytes, limit {settings.max_file_mb} MB")
    if size == 0:
        raise ServiceError(422, "input_empty", "file is empty")
    return path


# ------------------------------------------------------------------ run


def run(graph: Any, settings: Settings, *, file_location: str, client: str | None = None,
        usecase: str | None = None, version: str | None = None, thread_id: str | None = None,
        spool_run: str | None = None) -> Outcome:
    """Run the graph to its end and build the result. Never raises for a run-level failure.

    With ``thread_id`` (a job id) and a checkpointing graph, an interrupted run
    resumes from its last checkpoint instead of starting again.
    """
    request = {"file_location": file_location, "client": client, "usecase": usecase, "version": version}
    t0 = time.time()
    config = {"configurable": {"thread_id": thread_id}} if thread_id else {}
    try:
        state = None
        if thread_id and getattr(graph, "checkpointer", None) is not None:
            snap = graph.get_state(config)
            if snap.next:                       # a checkpoint mid-run: carry on from it
                state = graph.invoke(None, {**config, "max_concurrency": _parallel(snap.values)})
        if state is None:
            state = graph.invoke({**request, "spool_run": spool_run, "started": t0, "trace": [], "timings_ms": {}},
                                 {**config, "max_concurrency": 8})
    except ServiceError as exc:
        return _failed(settings, request, exc.code, str(exc), exc.detail, t0, status=exc.status)
    except ModelError as exc:
        status = {"model_unavailable": 503, "model_timeout": 504}.get(exc.code, 502)
        return _failed(settings, request, exc.code, str(exc), {"transient": exc.transient}, t0, status=status)
    return _result(settings, state, file_location, t0)


def _parallel(values: dict[str, Any]) -> int:
    cfg = values.get("cfg")
    return cfg.max_parallel if cfg is not None else 4


def _result(settings: Settings, state: dict[str, Any], file_location: str, t0: float) -> Outcome:
    cfg, sub, docs, records = state["cfg"], state["sub"], state["docs"], state["records"]
    df = cfg.decimal_format
    by_id = {d.doc_id: d for d in docs}
    data_out = [{name: to_json(v["value"], df) for name, v in rec["fields"].items()} for rec in records]
    extended = {
        "data": data_out,
        "status": "extracted",
        "flagged": state["flagged"],
        "confidence": state["overall"],
        "flags": state["reasons"],
        "records": [{"data": data, "flagged": rec["flagged"], "confidence": rec["confidence"],
                     "flags": rec["reasons"],
                     "fields": {name: field_out(v, by_id, df) for name, v in rec["fields"].items()}}
                    for data, rec in zip(data_out, records)],
        "metadata": {
            "audit_id": state["audit_id"],
            "engine_version": ENGINE_VERSION,
            "processed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "config": cfg.ref(),
            "model": {"provider": state["provider"], "name": state["model_name"],
                      **({"call": state["model_call"]} if state.get("model_call") else {})},
            "input": {"file_location": file_location, "name": sub.name, "kind": sub.kind,
                      "bytes": sub.size, "sha256": sub.data_sha256,
                      "subject": sub.subject, "sender": sub.sender, "message_id": sub.message_id},
            "documents": [{"id": d.doc_id, "source": d.source, "kind": d.kind, "status": d.status,
                           "sha256": d.sha256, "blocks": len(d.blocks), "notes": d.notes,
                           **({"reason": d.reason} if d.reason else {})} for d in docs],
            "skipped": sub.skipped,
            "classification": _classification(state.get("classification")),
            **({"expanded": state["expand_report"]} if state.get("expand_report") else {}),
            **({"transform": state["transform_report"]} if state.get("transform_report") else {}),
            "skills_applied": state.get("skills_applied") or [],
            "keys_used": sub.keys_used,
            "record_count": len(records),
            "record_key": cfg.dictionary.record_key,
            "cost": state.get("cost") or {},
            **({"model_error": state["model_error"]} if state.get("model_error") else {}),
            "graph": {"path": state["trace"]},
            "timings_ms": state["timings_ms"],
            "elapsed_ms": round((time.time() - t0) * 1000, 1),
        },
    }
    _write_audit(settings, extended)
    return Outcome(data=data_out, extended=extended, flagged=state["flagged"], audit_id=state["audit_id"],
                   flags=state["reasons"], cfg=cfg, docs=docs)


def _classification(c: dict[str, Any] | None) -> dict[str, Any]:
    """The classification outcome for the result: type, score, status and per-type scores."""
    if not c:
        return {"status": "unclassified", "type": None}
    out = {k: c[k] for k in ("status", "type", "score", "runner_up", "nearest") if c.get(k) is not None}
    out["scores"] = {t: {"score": v["score"], "threshold": v["threshold"], "matched": v["matched"],
                         **({"negative_signals": v["negative_signals"]} if v.get("negative_signals") else {})}
                     for t, v in (c.get("scores") or {}).items()}
    return out


def _failed(settings: Settings, request: dict[str, Any], code: str, message: str, detail: dict[str, Any],
            t0: float, status: int = 500) -> Outcome:
    """A run that could not extract still ends in a result, flagged with the error (RT-06)."""
    audit_id = "run_" + hashlib.sha256(json.dumps({**request, "error": code}, sort_keys=True).encode()
                                       ).hexdigest()[:20]
    extended = {"data": [], "status": "extracted", "flagged": True, "confidence": 0.0,
                "flags": [f"error:{code}"], "records": [],
                "error": {"code": code, "message": message, "detail": detail, "status": status},
                "metadata": {"audit_id": audit_id, "engine_version": ENGINE_VERSION,
                             "processed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                             "input": {"file_location": request["file_location"]},
                             "request": request, "record_count": 0,
                             "elapsed_ms": round((time.time() - t0) * 1000, 1)}}
    _write_audit(settings, extended)
    return Outcome(data=[], extended=extended, flagged=True, audit_id=audit_id, flags=[f"error:{code}"])


# ------------------------------------------------------------------ result files


def output_formats(settings: Settings, requested: list[str] | None, cfg: Any) -> list[str]:
    """Request, then the config's ``output.formats``, then OUTPUT_FORMATS (default csv)."""
    from extractor_tools.writers import normalise
    if requested is not None:
        chosen = requested
    elif cfg is not None and isinstance((cfg.manifest.get("output") or {}).get("formats"), list):
        chosen = cfg.manifest["output"]["formats"]
    else:
        chosen = list(settings.output_formats)
    out: list[str] = []
    for f in chosen:
        n = normalise(f)
        if n not in out:
            out.append(n)
    return out


def write_outputs(settings: Settings, extended: dict[str, Any], formats: list[str], name: str,
                  request: dict[str, Any]) -> list[dict[str, Any]]:
    """Write the result in each format under OUTPUT_ROOT; record what was written in metadata.outputs.

    A file that cannot be written does not fail the job: it is listed with its
    error and the result is flagged ``output_failed:<format>``.
    """
    if settings.output_root is None:
        return []
    from extractor_tools.writers import write
    md = extended.setdefault("metadata", {})
    cfg = md.get("config") or {}
    client = cfg.get("client") or request.get("client") or "_unresolved"
    usecase = cfg.get("usecase") or request.get("usecase") or "_unresolved"
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    folder = Path(settings.output_root) / client / usecase / day
    written = []
    for fmt in formats:
        try:
            data, media, ext = write(extended, fmt)
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / f"{name}.{ext}"
            tmp = path.with_name(f".{path.name}.tmp")
            tmp.write_bytes(data)
            tmp.replace(path)
            written.append({"format": fmt, "path": path.relative_to(settings.output_root).as_posix(),
                            "media_type": media, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()})
        except Exception as exc:                          # noqa: BLE001 - reported, never fatal
            written.append({"format": fmt, "error": f"{type(exc).__name__}: {exc}"[:300]})
            flag = f"output_failed:{fmt}"
            extended["flags"] = list(extended.get("flags") or []) + [flag]
            extended["flagged"] = True
    md["outputs"] = written
    return written


def _write_audit(settings: Settings, extended: dict[str, Any]) -> None:
    if settings.audit_dir:
        settings.audit_dir.mkdir(parents=True, exist_ok=True)
        path = settings.audit_dir / f"{extended['metadata']['audit_id']}.json"
        path.write_text(json.dumps(extended, indent=1, default=str))
