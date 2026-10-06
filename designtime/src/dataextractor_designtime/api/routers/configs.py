"""Config versions: the runtime's config folders, their record, sign-off, release and rollback.

The folders under RUNTIME_CONFIG_ROOT are the source of truth; these routes
read them, and change them only by writing ``releases.json`` (release,
rollback, reject) or a level's ingestion lookups. Version folders themselves
are written only by the authoring run and pattern learning.
"""

from __future__ import annotations

import difflib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import PlainTextResponse

from ... import configroot
from ...agents.base import AgentError
from ...config import Settings
from ...records import Record
from ...registry.configs import ConfigRegistry
from ...registry.errors import RegistryError
from ..deps import as_http, registry_dep, settings_dep
from ..typed import docs, json_body

router = APIRouter(tags=["configs"])

MAX_FILE_BYTES = 512 * 1024
LOOKUP_KEYS = ("ignore_names", "ignore_hashes", "ignore_kinds", "keep_names", "keep_hashes")
_HASH = re.compile(r"^(?:md5:[0-9a-f]{32}|(?:sha256:)?[0-9a-f]{64}|[0-9a-f]{32})$")


@dataclass(kw_only=True)
class SignoffRequest(Record):
    identity: str
    note: str | None = None


@dataclass(kw_only=True)
class ReleaseRequest(Record):
    by: str
    note: str | None = None
    #: Release although an evaluation gate failed (DT-34); needs a note.
    accept_gate_failure: bool = False


@dataclass(kw_only=True)
class RollbackRequest(Record):
    by: str
    note: str | None = None
    #: The version to go back to; default: the one released before the active one.
    to: str | None = None


@dataclass(kw_only=True)
class RejectRequest(Record):
    by: str
    note: str | None = None


@dataclass(kw_only=True)
class LookupsUpdate(Record):
    """One level's ingestion.json. Leave client (and usecase) out for the global level."""

    client: str | None = None
    usecase: str | None = None
    ignore_names: list[str] = field(default_factory=list)
    ignore_hashes: list[str] = field(default_factory=list)
    ignore_kinds: list[str] = field(default_factory=list)
    keep_names: list[str] = field(default_factory=list)
    keep_hashes: list[str] = field(default_factory=list)
    description: str | None = None
    by: str = "designtime"


def _root(settings: Settings) -> Path:
    return Path(settings.runtime_config_root)


def _version_folder(settings: Settings, client: str, usecase: str, version: str) -> Path:
    for kind, value in (("client", client), ("usecase", usecase)):
        configroot.check_segment(kind, value)
    if not configroot.SEMVER.match(version):
        raise HTTPException(400, {"code": "version_invalid", "message": f"{version!r} is not a version"})
    folder = _root(settings) / client / usecase / version
    if not folder.is_dir():
        raise HTTPException(404, {"code": "version_not_found", "message": f"no config {client}/{usecase}/{version}"})
    return folder


def _row_view(row) -> dict[str, Any] | None:
    if row is None:
        return None
    return {"origin": row.origin, "base_version": row.base_version, "run_id": row.run_id, "sha256": row.sha256,
            "evaluation": row.evaluation, "gates_passed": row.gates_passed, "created_by": row.created_by,
            "created_at": row.created_at.isoformat(),
            "signoffs": [{"identity": s.identity, "note": s.note, "at": s.created_at.isoformat()}
                         for s in row.signoffs]}


def _guard(fn, *args, **kw):
    try:
        return fn(*args, **kw)
    except (RegistryError, AgentError) as exc:
        raise as_http(exc) from exc


@router.get("/configs", summary="Every use case in the config root, with its versions and what is served")
def list_configs(settings: Settings = Depends(settings_dep), registry: ConfigRegistry = Depends(registry_dep)):
    root = _root(settings)
    out = []
    for client, usecase in configroot.usecases(root):
        d = configroot.describe(root, client, usecase)
        rows = {r.version: r for r in registry.list(client, usecase)}
        for v in d["versions"]:
            r = rows.get(v["version"])
            v["signoffs"] = len(r.signoffs) if r else 0
            v["gates_passed"] = r.gates_passed if r else None
            v["recorded"] = r is not None
        out.append(d)
    return {"root": str(root), "defaults": configroot.defaults(root), "usecases": out}


@router.get("/configs/{client}/{usecase}", summary="One use case: versions, releases.json and the release log")
def get_usecase(client: str, usecase: str, settings: Settings = Depends(settings_dep),
                registry: ConfigRegistry = Depends(registry_dep)):
    configroot.check_segment("client", client)
    configroot.check_segment("usecase", usecase)
    root = _root(settings)
    if not configroot.versions(root, client, usecase):
        raise HTTPException(404, {"code": "usecase_not_found", "message": f"no use case {client}/{usecase}"})
    d = configroot.describe(root, client, usecase)
    rows = {r.version: r for r in registry.list(client, usecase)}
    for v in d["versions"]:
        v["record"] = _row_view(rows.get(v["version"]))
    d["release_log"] = [{"action": r.action, "version": r.version, "previous": r.previous, "by": r.by,
                         "note": r.note, "gate_override": r.gate_override, "at": r.created_at.isoformat()}
                        for r in registry.releases(client, usecase)]
    return d


@router.get("/configs/{client}/{usecase}/{version}", summary="One version: manifest, files, provenance, record")
def get_version(client: str, usecase: str, version: str, settings: Settings = Depends(settings_dep),
                registry: ConfigRegistry = Depends(registry_dep)):
    folder = _version_folder(settings, client, usecase, version)
    root = _root(settings)
    rel = configroot.read_releases(root, client, usecase)
    row = registry.get(client, usecase, version)
    sha = configroot.folder_sha256(folder)
    return {"client": client, "usecase": usecase, "version": version,
            "status": configroot.status_of(rel, version),
            "manifest": json.loads((folder / "manifest.json").read_text(encoding="utf-8")),
            "provenance": configroot.provenance(root, client, usecase, version),
            "files": [{"path": p.relative_to(folder).as_posix(), "bytes": p.stat().st_size}
                      for p in sorted(folder.rglob("*")) if p.is_file() and not p.name.startswith(".")],
            "sha256": sha, "intact": row is None or row.sha256 == sha, "record": _row_view(row)}


@router.get("/configs/{client}/{usecase}/{version}/files/{path:path}", response_class=PlainTextResponse,
            summary="The text of one file in a version folder")
def get_file(client: str, usecase: str, version: str, path: str, settings: Settings = Depends(settings_dep)):
    folder = _version_folder(settings, client, usecase, version).resolve()
    target = (folder / path).resolve()
    if folder not in target.parents or not target.is_file():
        raise HTTPException(404, {"code": "file_not_found", "message": path})
    if target.stat().st_size > MAX_FILE_BYTES:
        raise HTTPException(413, {"code": "file_too_large", "message": path})
    return target.read_text(encoding="utf-8", errors="replace")


@router.get("/configs/{client}/{usecase}/{version}/diff", response_class=PlainTextResponse,
            summary="Unified diff of every text file against another version (default: the active one)")
def diff_version(client: str, usecase: str, version: str, against: str | None = Query(None),
                 settings: Settings = Depends(settings_dep)):
    new = _version_folder(settings, client, usecase, version)
    against = against or configroot.served_version(_root(settings), client, usecase)
    if not against or against == version:
        return ""
    old = _version_folder(settings, client, usecase, against)
    paths = sorted({p.relative_to(f).as_posix() for f in (old, new) for p in f.rglob("*") if p.is_file()})
    chunks = []
    for rel in paths:
        a = (old / rel).read_text(encoding="utf-8", errors="replace").splitlines(True) if (old / rel).is_file() else []
        b = (new / rel).read_text(encoding="utf-8", errors="replace").splitlines(True) if (new / rel).is_file() else []
        chunks.extend(difflib.unified_diff(a, b, f"{against}/{rel}", f"{version}/{rel}"))
    return "".join(chunks)


@router.get("/configs/{client}/{usecase}/{version}/verify", summary="Does the folder still match what was recorded?")
def verify_version(client: str, usecase: str, version: str, settings: Settings = Depends(settings_dep),
                   registry: ConfigRegistry = Depends(registry_dep)):
    _version_folder(settings, client, usecase, version)
    return _guard(registry.verify, client, usecase, version)


@router.post("/configs/{client}/{usecase}/{version}/signoff", summary="Sign off a version (CTR-19)",
             **docs(SignoffRequest, status=201))
def signoff(client: str, usecase: str, version: str,
            payload: SignoffRequest = Depends(json_body(SignoffRequest)),
            settings: Settings = Depends(settings_dep), registry: ConfigRegistry = Depends(registry_dep)):
    _version_folder(settings, client, usecase, version)
    s = _guard(registry.sign_off, client, usecase, version, payload.identity, payload.note)
    return {"version": version, "identity": s.identity, "note": s.note}


@router.post("/configs/{client}/{usecase}/{version}/release",
             summary="Make a version the one the runtime serves (rewrites releases.json)", **docs(ReleaseRequest))
def release(client: str, usecase: str, version: str, payload: ReleaseRequest = Depends(json_body(ReleaseRequest)),
            settings: Settings = Depends(settings_dep), registry: ConfigRegistry = Depends(registry_dep)):
    _version_folder(settings, client, usecase, version)
    return _guard(registry.release, client, usecase, version, payload.by, payload.note,
                  payload.accept_gate_failure)


@router.post("/configs/{client}/{usecase}/{version}/reject",
             summary="Set a candidate aside: never 'latest', never built on by learning", **docs(RejectRequest))
def reject(client: str, usecase: str, version: str, payload: RejectRequest = Depends(json_body(RejectRequest)),
           settings: Settings = Depends(settings_dep), registry: ConfigRegistry = Depends(registry_dep)):
    _version_folder(settings, client, usecase, version)
    return _guard(registry.reject, client, usecase, version, payload.by, payload.note)


@router.post("/configs/{client}/{usecase}/rollback", summary="Serve the previously released version again",
             **docs(RollbackRequest))
def rollback(client: str, usecase: str, payload: RollbackRequest = Depends(json_body(RollbackRequest)),
             registry: ConfigRegistry = Depends(registry_dep)):
    configroot.check_segment("client", client)
    configroot.check_segment("usecase", usecase)
    return _guard(registry.rollback, client, usecase, payload.by, payload.note, payload.to)


# ---------------------------------------------------------------------- ingestion lookups


def _lookup_folder(root: Path, client: str | None, usecase: str | None) -> tuple[str, Path]:
    if usecase and not client:
        raise HTTPException(400, {"code": "client_required", "message": "a use-case level needs its client"})
    if client:
        configroot.check_segment("client", client)
    if usecase:
        configroot.check_segment("usecase", usecase)
        return "usecase", root / client / usecase / "lookups"
    if client:
        return "client", root / client / "lookups"
    return "global", root / "lookups"


@router.get("/lookups", summary="Ingestion lookups at the usecase, client and global levels")
def get_lookups(client: str | None = None, usecase: str | None = None, settings: Settings = Depends(settings_dep)):
    root = _root(settings)
    wanted = ([(client, usecase)] if client and usecase else []) + ([(client, None)] if client else []) + [(None, None)]
    levels = []
    for c, u in wanted:
        level, folder = _lookup_folder(root, c, u)
        path = folder / "ingestion.json"
        data = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None
        tables = sorted(p.relative_to(folder).as_posix() for p in (folder / "tables").rglob("*.json")) \
            if (folder / "tables").is_dir() else []
        levels.append({"level": level, "path": str(folder), "ingestion": data,
                       "decision": (folder / "ingestion.decision.json").is_file(),
                       # ZEN rules for deterministic transformation and lookup (runtime transform.py)
                       "transform": {"entry": (folder / "transform.decision.json").is_file(), "tables": tables}})
    return {"client": client, "usecase": usecase, "levels": levels}


@router.put("/lookups", summary="Replace one level's ingestion.json (global, client, or client + usecase)",
            **docs(LookupsUpdate))
def put_lookups(payload: LookupsUpdate = Depends(json_body(LookupsUpdate)),
                settings: Settings = Depends(settings_dep)):
    level, folder = _lookup_folder(_root(settings), payload.client, payload.usecase)
    for h in payload.ignore_hashes + payload.keep_hashes:
        if not _HASH.match(str(h).strip().lower()):
            raise HTTPException(422, {"code": "invalid_hash", "message": f"{h!r} is not a sha256 or md5:<hex> hash"})
    for name in payload.ignore_names + payload.keep_names:
        if not str(name).strip():
            raise HTTPException(422, {"code": "invalid_name", "message": "name entries must be non-empty"})
        if str(name).lower().startswith("re:"):
            try:
                re.compile(str(name)[3:])
            except re.error as exc:
                raise HTTPException(422, {"code": "invalid_regex", "message": f"{name!r}: {exc}"}) from exc
    data = {k: getattr(payload, k) for k in LOOKUP_KEYS if getattr(payload, k)}
    if payload.description:
        data["description"] = payload.description
    folder.mkdir(parents=True, exist_ok=True)
    tmp = folder / ".ingestion.json.tmp"
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    tmp.replace(folder / "ingestion.json")
    return {"level": level, "path": str(folder), "ingestion": data}
