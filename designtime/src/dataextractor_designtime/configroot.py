"""The runtime's config folders: the single source of truth for what extracts what.

Everything design-time produces lands here as a new version folder, never as
an edit to an existing one::

    <root>/<client>/<usecase>/<version>/      manifest.json, schema.json, schemas/, rules/,
                                              prompts/, skills/, provenance.json
    <root>/<client>/<usecase>/releases.json   which version is served, and the history

Both producers write through ``publish_version``: the authoring run (a corpus
becomes a new use case, or a redesign of one: the next minor version) and
pattern learning (one sample refines the latest version: the next patch).
A published version is a *candidate*. Nothing goes live until a person
releases it (``release``), which rewrites ``releases.json``; ``rollback``
re-releases the previous version, ``reject`` sets a candidate aside so neither
the runtime's "latest" nor learning builds on it.

The first time design-time publishes into a use case that has no
``releases.json`` it writes one that pins the version being served at that
moment ("adopt"), so publishing a candidate never changes what is served.

The Postgres registry keeps the record about these folders (registry/configs.py):
who produced each version and from what, its evaluation, its sign-offs and
every release. The folders win any disagreement: the registry stores each
version's SHA-256 and a release is refused when the folder no longer matches.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .agents.base import AgentError

SEMVER = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
RESERVED = frozenset({"lookups"})
RELEASES_FILE = "releases.json"
PROVENANCE_FILE = "provenance.json"


class ConfigRootError(AgentError):
    code = "config_root_error"
    status = 409


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def check_segment(kind: str, value: str) -> str:
    if not isinstance(value, str) or not SEGMENT.match(value) or value in RESERVED:
        raise ConfigRootError(f"{kind} {value!r} is not a valid folder name", code="config_invalid_name",
                              status=400, detail={kind: value})
    return value


def semver_key(v: str) -> tuple[int, int, int]:
    m = SEMVER.match(v)
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else (-1, -1, -1)


def folder_sha256(folder: Path) -> str:
    """The runtime's version hash (config_store.folder_sha256): every file, path and bytes."""
    h = hashlib.sha256()
    for p in sorted(x for x in Path(folder).rglob("*") if x.is_file()):
        rel = p.relative_to(folder).as_posix()
        if rel.startswith(".") or "/." in rel:
            continue
        h.update(rel.encode() + b"\0" + p.read_bytes() + b"\0")
    return h.hexdigest()


def _ignore(_dir: str, names: list[str]) -> list[str]:
    return [n for n in names if n.startswith(".") or n == "__pycache__"]


# ---------------------------------------------------------------------- discovery


def versions(root: Path, client: str, usecase: str) -> list[str]:
    folder = Path(root) / client / usecase
    if not folder.is_dir():
        return []
    return sorted((p.name for p in folder.iterdir() if p.is_dir() and SEMVER.match(p.name)), key=semver_key)


def usecases(root: Path) -> list[tuple[str, str]]:
    root = Path(root)
    out = []
    if not root.is_dir():
        return out
    for c in sorted(p for p in root.iterdir() if p.is_dir() and SEGMENT.match(p.name) and p.name not in RESERVED):
        for u in sorted(p for p in c.iterdir() if p.is_dir() and SEGMENT.match(p.name) and p.name not in RESERVED):
            if versions(root, c.name, u.name):
                out.append((c.name, u.name))
    return out


def defaults(root: Path) -> dict[str, str]:
    path = Path(root) / "defaults.json"
    if not path.is_file():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return {k: str(data[k]) for k in ("client", "usecase", "version") if data.get(k)}


def read_releases(root: Path, client: str, usecase: str) -> dict[str, Any] | None:
    path = Path(root) / client / usecase / RELEASES_FILE
    if not path.is_file():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    return {"active": data.get("active"), "rejected": list(data.get("rejected") or []),
            "history": list(data.get("history") or [])}


def status_of(rel: dict[str, Any] | None, version: str) -> str:
    """active | candidate | retired | rejected | unmanaged (mirrors the runtime's config_store)."""
    if rel is None:
        return "unmanaged"
    if version in rel.get("rejected", []):
        return "rejected"
    active = rel.get("active")
    if version == active:
        return "active"
    if active is None or semver_key(version) > semver_key(active):
        return "candidate"
    return "retired"


def served_version(root: Path, client: str, usecase: str) -> str | None:
    """What the runtime serves when a request names no version (config_store.resolve)."""
    rel = read_releases(root, client, usecase)
    if rel is not None:
        return rel.get("active")
    d = defaults(root)
    if d.get("version") and (d.get("client"), d.get("usecase")) == (client, usecase) and \
            (Path(root) / client / usecase / d["version"]).is_dir():
        return d["version"]
    vs = versions(root, client, usecase)
    return vs[-1] if vs else None


def latest(root: Path, client: str, usecase: str) -> str | None:
    """The newest version that is not rejected: what learning builds on and "latest" serves."""
    rel = read_releases(root, client, usecase) or {}
    vs = [v for v in versions(root, client, usecase) if v not in rel.get("rejected", [])]
    return vs[-1] if vs else None


def provenance(root: Path, client: str, usecase: str, version: str) -> dict[str, Any]:
    path = Path(root) / client / usecase / version / PROVENANCE_FILE
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    except ValueError:
        return {}


def describe(root: Path, client: str, usecase: str) -> dict[str, Any]:
    rel = read_releases(root, client, usecase)
    out = {"client": client, "usecase": usecase, "managed": rel is not None,
           "active": (rel or {}).get("active"), "served": served_version(root, client, usecase),
           "history": (rel or {}).get("history", []), "versions": []}
    for v in versions(root, client, usecase):
        p = provenance(root, client, usecase, v)
        out["versions"].append({"version": v, "status": status_of(rel, v), "origin": p.get("origin", "manual"),
                                "base_version": p.get("base_version"), "run_id": p.get("run_id"),
                                "created_at": p.get("created_at"), "created_by": p.get("created_by"),
                                "summary": p.get("summary")})
    return out


# ---------------------------------------------------------------------- writing


@contextmanager
def _locked(root: Path, client: str, usecase: str, timeout_s: float = 30.0) -> Iterator[None]:
    """One writer per use case across processes: an exclusive lock file next to releases.json."""
    folder = Path(root) / client / usecase
    folder.mkdir(parents=True, exist_ok=True)
    lock = folder / ".releases.lock"
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            try:                                         # a lock left by a dead process
                if time.time() - lock.stat().st_mtime > 120:
                    lock.unlink(missing_ok=True)
                    continue
            except FileNotFoundError:
                continue
            if time.monotonic() > deadline:
                raise ConfigRootError(f"{client}/{usecase} is locked by another writer", code="config_locked")
            time.sleep(0.05)
    try:
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        yield
    finally:
        lock.unlink(missing_ok=True)


def _write_json(path: Path, data: Any) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _ensure_managed(root: Path, client: str, usecase: str, by: str) -> dict[str, Any]:
    rel = read_releases(root, client, usecase)
    if rel is not None:
        return rel
    serving = served_version(root, client, usecase)
    rel = {"active": serving, "rejected": [], "history": []}
    if serving:
        rel["history"].append({"action": "adopt", "version": serving, "by": by, "at": now(),
                               "note": "pinned the version being served before the first candidate"})
    _write_json(Path(root) / client / usecase / RELEASES_FILE, rel)
    return rel


def next_version(base: str | None, bump: str, taken: list[str]) -> str:
    if not base:
        candidate = "1.0.0"
    else:
        m = SEMVER.match(base)
        if not m:
            raise ConfigRootError(f"base version {base!r} is not semantic", code="version_invalid")
        major, minor, patch = (int(x) for x in m.groups())
        candidate = {"major": f"{major + 1}.0.0", "minor": f"{major}.{minor + 1}.0",
                     "patch": f"{major}.{minor}.{patch + 1}"}[bump]
    while candidate in taken:
        major, minor, patch = semver_key(candidate)
        candidate = {"major": f"{major + 1}.0.0", "minor": f"{major}.{minor + 1}.0",
                     "patch": f"{major}.{minor}.{patch + 1}"}[bump]
    return candidate


def publish_version(candidate: Path, root: Path, client: str, usecase: str, *, base: str | None, bump: str,
                    origin: str, created_by: str, run_id: str | None = None,
                    summary: dict[str, Any] | None = None) -> dict[str, Any]:
    """Copy a tested candidate folder into the root as a new version; returns its record.

    The version is the next free one on ``bump`` from ``base`` (or 1.0.0). The use
    case is put under ``releases.json`` first if it was not, pinning what is served,
    so the new version is a candidate and serving does not change.
    """
    check_segment("client", client)
    check_segment("usecase", usecase)
    root = Path(root)
    with _locked(root, client, usecase):
        _ensure_managed(root, client, usecase, created_by)
        taken = versions(root, client, usecase)
        version = next_version(base, bump, taken)
        dst = root / client / usecase / version
        dst.mkdir(parents=True, exist_ok=False)
        try:
            for item in Path(candidate).iterdir():
                if item.name.startswith("."):
                    continue
                if item.is_dir():
                    shutil.copytree(item, dst / item.name, ignore=_ignore)
                else:
                    shutil.copy2(item, dst / item.name)
            manifest_path = dst / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest.update({"client": client, "usecase": usecase, "version": version})
            _write_json(manifest_path, manifest)
            prov = {"origin": origin, "base_version": base, "run_id": run_id, "created_by": created_by,
                    "created_at": now(), "summary": summary or {}}
            _write_json(dst / PROVENANCE_FILE, prov)
        except Exception:
            shutil.rmtree(dst, ignore_errors=True)
            raise
    return {"client": client, "usecase": usecase, "version": version, "path": str(dst),
            "sha256": folder_sha256(dst), "status": "candidate", **prov}


def _change(root: Path, client: str, usecase: str, action: str, version: str, by: str,
            note: str | None, mutate) -> dict[str, Any]:
    check_segment("client", client)
    check_segment("usecase", usecase)
    root = Path(root)
    if not (root / client / usecase / version).is_dir():
        raise ConfigRootError(f"no version {client}/{usecase}/{version}", code="version_not_found", status=404)
    with _locked(root, client, usecase):
        rel = _ensure_managed(root, client, usecase, by)
        previous = rel.get("active")
        mutate(rel)
        rel["history"].append({"action": action, "version": version, "previous": previous, "by": by,
                               "at": now(), **({"note": note} if note else {})})
        _write_json(root / client / usecase / RELEASES_FILE, rel)
    return {"client": client, "usecase": usecase, "action": action, "version": version, "previous": previous,
            "active": rel.get("active"), "by": by}


def release(root: Path, client: str, usecase: str, version: str, by: str, note: str | None = None,
            action: str = "release") -> dict[str, Any]:
    def mutate(rel: dict[str, Any]) -> None:
        if version in rel["rejected"]:
            raise ConfigRootError(f"{version} was rejected; it cannot be released", code="version_rejected")
        if rel.get("active") == version and action == "release":
            raise ConfigRootError(f"{version} is already active", code="already_active")
        rel["active"] = version
    return _change(root, client, usecase, action, version, by, note, mutate)


def previous_release(root: Path, client: str, usecase: str) -> str | None:
    """The version that was active before the current one (skipping rejected ones)."""
    rel = read_releases(root, client, usecase)
    if not rel or not rel.get("active"):
        return None
    seen = []
    for h in rel["history"]:
        if h.get("action") in ("release", "rollback", "adopt") and h.get("version"):
            seen.append(h["version"])
    active = rel["active"]
    for v in reversed(seen):
        if v != active and v not in rel["rejected"] and (Path(root) / client / usecase / v).is_dir():
            return v
    return None


def reject(root: Path, client: str, usecase: str, version: str, by: str, note: str | None = None) -> dict[str, Any]:
    def mutate(rel: dict[str, Any]) -> None:
        if rel.get("active") == version:
            raise ConfigRootError(f"{version} is active; roll back before rejecting it", code="version_active")
        if version not in rel["rejected"]:
            rel["rejected"].append(version)
    return _change(root, client, usecase, "reject", version, by, note, mutate)
