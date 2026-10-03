"""Extraction configs: one folder per client, use case and version.

    <CONFIG_ROOT>/
      defaults.json                       {"client": "...", "usecase": "...", "version": "..."}
      <client>/<usecase>/<version>/
        manifest.json                     model, skills order, thresholds, evidence, output
        schema.json                       the data dictionary (see schema.py)
        prompts/system.md                 the system prompt
        skills/<name>.md                  one file per skill, used in manifest order

How a request picks its folder (each part the request leaves out):
  client   -> the default client
  usecase  -> the default use case when the client is the default client,
              otherwise the client's only use case (more than one = error)
  version  -> the default version when client and use case are the defaults,
              otherwise the highest semantic version; "latest" asks for that too

A version folder is treated as immutable: its SHA-256 over every file is
recorded with each extraction so a result can be traced to the exact
prompt, skills and dictionary that produced it.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import ConfigError
from .schema import DataDictionary, load_dictionary

_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_SEMVER = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")

DEFAULT_EVIDENCE = {"rows": 200, "tail_rows": 10, "pages": 10, "tail_pages": 1,
                    "max_sheets": 5, "max_chars_per_document": 60_000}
DEFAULT_INTAKE = {"max_zip_members": 200, "max_zip_uncompressed_mb": 200,
                  "max_compression_ratio": 100, "max_zip_depth": 1}


@dataclass(frozen=True)
class Skill:
    name: str
    body: str


@dataclass(frozen=True)
class ExtractionConfig:
    client: str
    usecase: str
    version: str
    path: Path
    sha256: str
    manifest: dict[str, Any]
    dictionary: DataDictionary
    system_prompt: str
    skills: tuple[Skill, ...]
    resolved_by: dict[str, str]

    @property
    def model(self) -> dict[str, Any]:
        return dict(self.manifest.get("model") or {"provider": "stub"})

    @property
    def accept_at(self) -> float:
        return float((self.manifest.get("thresholds") or {}).get("accept_at", 0.8))

    @property
    def evidence(self) -> dict[str, Any]:
        return {**DEFAULT_EVIDENCE, **(self.manifest.get("evidence") or {})}

    @property
    def intake(self) -> dict[str, Any]:
        return {**DEFAULT_INTAKE, **(self.manifest.get("intake") or {})}

    @property
    def date_order(self) -> str | None:
        return (self.manifest.get("locale") or {}).get("date_order")

    @property
    def decimal_format(self) -> str:
        return (self.manifest.get("output") or {}).get("decimal_format", "number")

    def ref(self) -> dict[str, Any]:
        return {"client": self.client, "usecase": self.usecase, "version": self.version,
                "sha256": self.sha256, "resolved_by": self.resolved_by}


def _semver_key(v: str) -> tuple[int, int, int]:
    m = _SEMVER.match(v)
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else (-1, -1, -1)


def _check_segment(kind: str, value: str) -> str:
    if not isinstance(value, str) or not _SEGMENT.match(value) or value in (".", ".."):
        raise ConfigError("config_invalid_name", f"{kind} {value!r} is not a valid folder name",
                          {kind: value}, status=400)
    return value


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError("config_incomplete", f"missing {path.name}", {"path": str(path)}) from exc
    except json.JSONDecodeError as exc:
        raise ConfigError("config_invalid", f"{path.name} is not valid JSON: {exc}",
                          {"path": str(path)}) from exc


def folder_sha256(folder: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(x for x in folder.rglob("*") if x.is_file()):
        rel = p.relative_to(folder).as_posix()
        if rel.startswith(".") or "/." in rel:
            continue
        h.update(rel.encode() + b"\0" + p.read_bytes() + b"\0")
    return h.hexdigest()


class ConfigStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self._cache: dict[tuple[str, str], ExtractionConfig] = {}
        self._lock = threading.Lock()

    # ---------------------------------------------------------- discovery

    def defaults(self) -> dict[str, str]:
        path = self.root / "defaults.json"
        if not path.exists():
            return {}
        d = _read_json(path)
        return {k: str(d[k]) for k in ("client", "usecase", "version") if d.get(k)}

    def _dirs(self, path: Path) -> list[str]:
        if not path.is_dir():
            return []
        return sorted(p.name for p in path.iterdir() if p.is_dir() and _SEGMENT.match(p.name))

    def versions(self, client: str, usecase: str) -> list[str]:
        vs = [v for v in self._dirs(self.root / client / usecase) if _SEMVER.match(v)]
        return sorted(vs, key=_semver_key)

    def catalogue(self) -> list[dict[str, Any]]:
        out = []
        for client in self._dirs(self.root):
            for usecase in self._dirs(self.root / client):
                versions = self.versions(client, usecase)
                if versions:
                    out.append({"client": client, "usecase": usecase, "versions": versions,
                                "latest": versions[-1]})
        return out

    # ---------------------------------------------------------- resolution

    def resolve(self, client: str | None = None, usecase: str | None = None,
                version: str | None = None) -> ExtractionConfig:
        d = self.defaults()
        by: dict[str, str] = {}

        if client:
            by["client"] = "request"
        elif d.get("client"):
            client, by["client"] = d["client"], "default"
        else:
            raise ConfigError("config_not_found", "no client given and no defaults.json",
                              {"available": self.catalogue()}, status=404)
        _check_segment("client", client)

        if usecase:
            by["usecase"] = "request"
        elif d.get("usecase") and client == d.get("client"):
            usecase, by["usecase"] = d["usecase"], "default"
        else:
            options = [u for u in self._dirs(self.root / client) if self.versions(client, u)]
            if len(options) != 1:
                raise ConfigError("config_ambiguous" if options else "config_not_found",
                                  f"client {client!r} has {len(options)} use cases; pass usecase",
                                  {"usecases": options}, status=400 if options else 404)
            usecase, by["usecase"] = options[0], "only"
        _check_segment("usecase", usecase)

        if version and version != "latest":
            by["version"] = "request"
        elif not version and d.get("version") and (client, usecase) == (d.get("client"), d.get("usecase")):
            version, by["version"] = d["version"], "default"
        else:
            versions = self.versions(client, usecase)
            if not versions:
                raise ConfigError("config_not_found", f"no versions for {client}/{usecase}",
                                  {"available": self.catalogue()}, status=404)
            version, by["version"] = versions[-1], "latest"
        _check_segment("version", version)

        folder = self.root / client / usecase / version
        if not folder.is_dir():
            raise ConfigError("config_not_found", f"no config at {client}/{usecase}/{version}",
                              {"available": self.catalogue()}, status=404)
        return self._load(folder, client, usecase, version, by)

    # ---------------------------------------------------------- loading

    def _load(self, folder: Path, client: str, usecase: str, version: str,
              by: dict[str, str]) -> ExtractionConfig:
        sha = folder_sha256(folder)
        key = (str(folder), sha)
        with self._lock:
            cached = self._cache.get(key)
        if cached:
            return ExtractionConfig(**{**cached.__dict__, "resolved_by": dict(by)})

        manifest = _read_json(folder / "manifest.json")
        if not isinstance(manifest, dict):
            raise ConfigError("config_invalid", "manifest.json must be an object")
        dictionary = load_dictionary(_read_json(folder / "schema.json"))
        prompt_path = folder / "prompts" / "system.md"
        if not prompt_path.is_file():
            raise ConfigError("config_incomplete", "missing prompts/system.md", {"path": str(folder)})
        skills = []
        for name in manifest.get("skills", []):
            _check_segment("skill", name)
            p = folder / "skills" / f"{name}.md"
            if not p.is_file():
                raise ConfigError("config_incomplete", f"skill {name!r} listed but skills/{name}.md missing")
            skills.append(Skill(name, p.read_text(encoding="utf-8")))
        provider = (manifest.get("model") or {}).get("provider", "stub")
        if provider not in ("stub", "gateway"):
            raise ConfigError("config_invalid", f"model.provider must be 'stub' or 'gateway', got {provider!r}")
        cfg = ExtractionConfig(client=client, usecase=usecase, version=version, path=folder,
                               sha256=sha, manifest=manifest, dictionary=dictionary,
                               system_prompt=prompt_path.read_text(encoding="utf-8"),
                               skills=tuple(skills), resolved_by=dict(by))
        with self._lock:
            self._cache[key] = cfg
        return cfg

    def validate_all(self) -> list[dict[str, Any]]:
        """Load every version folder; returns problems instead of raising (startup check)."""
        problems = []
        for entry in self.catalogue():
            for v in entry["versions"]:
                try:
                    self.resolve(entry["client"], entry["usecase"], v)
                except ConfigError as exc:
                    problems.append({"config": f"{entry['client']}/{entry['usecase']}/{v}",
                                     **exc.to_dict()})
        return problems
