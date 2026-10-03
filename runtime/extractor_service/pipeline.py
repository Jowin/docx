"""Run one extraction: settings, file-location checks, and the call into the graph.

The steps themselves live in graph.py (a LangGraph state graph); verification
and scoring rules are in verify.py; every model call goes through gateway.py.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config_store import ConfigStore
from .errors import ServiceError
from .schema import to_json
from .verify import field_out

ENGINE_VERSION = "0.3.0"


@dataclass(frozen=True)
class Settings:
    config_root: Path
    input_root: Path
    audit_dir: Path | None = None
    model_provider: str | None = None      # override every config's provider, e.g. "stub"
    max_file_mb: int = 25

    @classmethod
    def from_env(cls) -> "Settings":
        here = Path(__file__).resolve().parent.parent
        audit = os.environ.get("AUDIT_DIR")
        return cls(config_root=Path(os.environ.get("CONFIG_ROOT", here / "configs")),
                   input_root=Path(os.environ.get("INPUT_ROOT", here / "data")),
                   audit_dir=Path(audit) if audit else None,
                   model_provider=os.environ.get("MODEL_PROVIDER") or None,
                   max_file_mb=int(os.environ.get("MAX_FILE_MB", "25")))


@dataclass
class Outcome:
    data: list[dict[str, Any]]
    extended: dict[str, Any]
    status: str
    audit_id: str
    reasons: list[str] = field(default_factory=list)
    cfg: Any = field(default=None, repr=False)            # the ExtractionConfig used
    docs: list[Any] = field(default_factory=list, repr=False)  # evidence documents read


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
        usecase: str | None = None, version: str | None = None) -> Outcome:
    state = graph.invoke({"file_location": file_location, "client": client, "usecase": usecase,
                          "version": version, "trace": [], "timings_ms": {}})
    cfg, sub, docs, records = state["cfg"], state["sub"], state["docs"], state["records"]
    df = cfg.decimal_format
    by_id = {d.doc_id: d for d in docs}
    data_out = [{name: to_json(v["value"], df) for name, v in rec["fields"].items()} for rec in records]
    extended = {
        "data": data_out,
        "status": state["status"],
        "confidence": state["overall"],
        "review_reasons": state["reasons"],
        "records": [{"data": data, "status": rec["status"], "confidence": rec["confidence"],
                     "review_reasons": rec["reasons"],
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
                      "subject": sub.subject, "sender": sub.sender},
            "documents": [{"id": d.doc_id, "source": d.source, "kind": d.kind, "status": d.status,
                           "sha256": d.sha256, "blocks": len(d.blocks), "notes": d.notes,
                           **({"reason": d.reason} if d.reason else {})} for d in docs],
            "skipped": sub.skipped,
            "record_count": len(records),
            "record_key": cfg.dictionary.record_key,
            "graph": {"path": state["trace"]},
            "timings_ms": state["timings_ms"],
        },
    }
    if settings.audit_dir:
        settings.audit_dir.mkdir(parents=True, exist_ok=True)
        (settings.audit_dir / f"{state['audit_id']}.json").write_text(json.dumps(extended, indent=1, default=str))
    return Outcome(data=data_out, extended=extended, status=state["status"],
                   audit_id=state["audit_id"], reasons=state["reasons"], cfg=cfg, docs=docs)
