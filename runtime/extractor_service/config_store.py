"""Extraction configs: one folder per client, use case and version.

    <CONFIG_ROOT>/
      defaults.json                       {"client": "...", "usecase": "...", "version": "..."}
      lookups/                            ingestion lookups for everyone (ingest_filter.py)
      <client>/lookups/                   ... for one client
      <client>/<usecase>/
        lookups/                          ... for one client's use case
        releases.json                     which version is live, and the release history
        <version>/
          manifest.json                   model, skills order, thresholds, evidence, output, types
          schema.json                     the data dictionary (see schema.py)
          schemas/<type>.json             more dictionaries, one per extra email type (optional)
          rules/detection.json            email-type detection rules, CTR-08 (optional, classify.py)
          prompts/system.md               the system prompt
          skills/<name>.md                one file per skill, used in manifest order
          provenance.json                 who or what produced this version (design-time writes it)

The config folders are the source of truth: design-time writes new versions
here (never edits one), and ``releases.json`` says which one is served.

**Email types.** A manifest may declare several email types, each with its
own dictionary, skills and accept threshold; ``skills`` at the top level are
shared by every type::

    "default_type": "invoice",
    "types": {
      "invoice":    {"schema": "schema.json", "skills": ["invoice-fields"],
                     "thresholds": {"accept_at": 0.85}},
      "remittance": {"schema": "schemas/remittance.json", "skills": []}
    }

The runtime's classify step picks the type with ``rules/detection.json``
(``ExtractionConfig.for_type``); with no ``types`` the config is a single type.

**Releases.** ``releases.json`` (written by design-time on a human's release
or rollback, never by learning on its own)::

    {"active": "1.0.1", "rejected": ["1.0.2"],
     "history": [{"action": "release", "version": "1.0.1", "by": "...", "at": "...", "note": "..."}]}

A version newer than the active one is a *candidate*: it is served only when a
request names it (or asks for "latest"), which is how candidates are tested.
A *rejected* candidate is never "latest"; it can still be named explicitly.

A skill is markdown for the model. It may open with a YAML front matter block
whose ``hints`` are machine-readable (labels and anchors per field, see
schema.apply_hints); design-time writes these when it learns a pattern, and
the hints are folded into the data dictionary so every extractor, the
deterministic stub included, uses them. The model sees the body only.

    ---
    name: acme-remittance
    kind: learned-pattern
    hints:
      fields:
        invoice_number: {labels: ["our ref"]}
        total_amount: {anchors: ["please pay"]}
    ---
    ## Skill: acme remittance advice
    ...

A skill may also carry ``applies_to`` (see scope.py): then its hints and body
are used only for documents that match it. ``ExtractionConfig.for_documents``
picks the skills for one run; ``dictionary`` (every hint applied) is the
config's full dictionary, used for validation and description.


How a request picks its folder (each part the request leaves out):
  client   -> the default client
  usecase  -> the default use case when the client is the default client,
              otherwise the client's only use case (more than one = error)
  version  -> the active version in releases.json when the use case has one
              (none active = config_not_released); otherwise the default
              version when client and use case are the defaults; otherwise the
              highest semantic version. "latest" always asks for the highest
              version that is not rejected.

A version folder is treated as immutable: its SHA-256 over every file is
recorded with each extraction so a result can be traced to the exact
prompt, skills and dictionary that produced it.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from . import scope
from .errors import ConfigError
from .schema import DataDictionary, apply_hints, load_dictionary

_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_SEMVER = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
#: Folder names with a meaning of their own; never a client or use case.
RESERVED = frozenset({"lookups"})
RELEASES_FILE = "releases.json"
DETECTION_FILE = "rules/detection.json"

DEFAULT_EVIDENCE = {"rows": 200, "tail_rows": 10, "pages": 10, "tail_pages": 1,
                    "max_sheets": 5, "max_chars_per_document": 60_000,
                    "ocr": True, "ocr_timeout_s": 60, "parse_timeout_s": 120,
                    "max_attachment_mb": 25}
DEFAULT_INTAKE = {"max_zip_members": 200, "max_zip_uncompressed_mb": 200,
                  "max_compression_ratio": 100, "max_zip_depth": 1,
                  "max_email_depth": 3, "ocr_images": False}
#: run_ceiling_s: RT-60's hard cap. max_cost_usd: RT-62's per-run model cost ceiling (unset = none).
DEFAULT_LIMITS = {"run_ceiling_s": 300, "max_cost_usd": None}


@dataclass(frozen=True)
class Skill:
    name: str
    body: str                                   # markdown after any front matter
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def hints(self) -> dict[str, Any]:
        return self.meta.get("hints") or {}

    @property
    def applies_to(self) -> dict[str, Any]:
        return self.meta.get("applies_to") or {}


_FRONT = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.S)


def parse_skill(name: str, text: str) -> Skill:
    """Split a skill file into its front matter (if any) and markdown body."""
    m = _FRONT.match(text)
    if not m:
        return Skill(name, text)
    try:
        meta = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError as exc:
        raise ConfigError("config_invalid", f"skill {name!r}: front matter is not valid YAML: {exc}") from exc
    if not isinstance(meta, dict):
        raise ConfigError("config_invalid", f"skill {name!r}: front matter must be a mapping")
    scope.validate(name, meta.get("applies_to"))
    return Skill(name, text[m.end():].lstrip("\r\n"), meta)


@dataclass(frozen=True)
class EmailType:
    """One email type of a multi-type config: its dictionary, skills and accept threshold."""
    name: str
    dictionary: DataDictionary
    base_dictionary: DataDictionary
    skills: tuple[Skill, ...]
    accept_at: float | None = None


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
    base_dictionary: DataDictionary | None = None     # the schema.json dictionary, before any hints
    root: Path | None = None                          # CONFIG_ROOT, for lookup data
    types: dict[str, EmailType] = field(default_factory=dict)   # empty = a single-type config
    detection: dict[str, Any] | None = None          # rules/detection.json, validated
    email_type: str | None = None                    # set by for_type
    release_status: str = "unmanaged"                # active | candidate | retired | unmanaged
    type_accept_at: float | None = None

    @property
    def default_type(self) -> str | None:
        if not self.types:
            return None
        named = self.manifest.get("default_type")
        return named if named in self.types else next(iter(self.types))

    def extractable(self, email_type: str | None) -> bool:
        """Whether this config extracts that type (a single-type config extracts whatever matched)."""
        return not self.types or email_type in self.types

    def for_type(self, email_type: str | None) -> "ExtractionConfig":
        """This config narrowed to one email type: its dictionary, skills and threshold."""
        if not self.types:
            return dataclasses.replace(self, email_type=email_type) if email_type else self
        t = self.types.get(email_type or "") or self.types[self.default_type]
        out = dataclasses.replace(self, email_type=t.name, dictionary=t.dictionary,
                                  base_dictionary=t.base_dictionary, skills=t.skills, type_accept_at=t.accept_at)
        if "_ingestion_filter" in self.__dict__:
            object.__setattr__(out, "_ingestion_filter", self.__dict__["_ingestion_filter"])
        return out

    def for_documents(self, facts: dict[str, Any]) -> tuple[DataDictionary, tuple[Skill, ...]]:
        """The dictionary and skills for one run: skills whose fingerprint matches (or that have none)."""
        chosen = tuple(s for s in self.skills if scope.applies(s.applies_to, facts))
        if len(chosen) == len(self.skills) or self.base_dictionary is None:
            return self.dictionary, chosen if self.base_dictionary is not None else self.skills
        return apply_hints(self.base_dictionary, [(s.name, s.hints) for s in chosen]), chosen

    @property
    def ingestion_filter(self):
        from .ingest_filter import IngestionFilter
        key = "_ingestion_filter"
        cached = self.__dict__.get(key)
        if cached is None:
            cached = IngestionFilter.load(self.root or self.path.parent.parent.parent, self.client, self.usecase)
            object.__setattr__(self, key, cached)
        return cached

    @property
    def model(self) -> dict[str, Any]:
        return dict(self.manifest.get("model") or {"provider": "stub"})

    @property
    def accept_at(self) -> float:
        if self.type_accept_at is not None:
            return float(self.type_accept_at)
        return float((self.manifest.get("thresholds") or {}).get("accept_at", 0.8))

    @property
    def classification(self) -> dict[str, Any]:
        return {"out_of_scope": "skip", "ambiguity_margin": 0.1, **(self.manifest.get("classification") or {})}

    @property
    def evidence(self) -> dict[str, Any]:
        return {**DEFAULT_EVIDENCE, **(self.manifest.get("evidence") or {})}

    @property
    def intake(self) -> dict[str, Any]:
        return {**DEFAULT_INTAKE, **(self.manifest.get("intake") or {})}

    @property
    def limits(self) -> dict[str, Any]:
        return {**DEFAULT_LIMITS, **(self.manifest.get("limits") or {})}

    @property
    def max_parallel(self) -> int:
        return int((self.manifest.get("concurrency") or {}).get("max_parallel", 4))

    @property
    def date_order(self) -> str | None:
        return (self.manifest.get("locale") or {}).get("date_order")

    @property
    def decimal_format(self) -> str:
        return (self.manifest.get("output") or {}).get("decimal_format", "number")

    def ref(self) -> dict[str, Any]:
        return {"client": self.client, "usecase": self.usecase, "version": self.version,
                "sha256": self.sha256, "resolved_by": self.resolved_by, "release_status": self.release_status,
                **({"email_type": self.email_type} if self.email_type else {})}


def _semver_key(v: str) -> tuple[int, int, int]:
    m = _SEMVER.match(v)
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else (-1, -1, -1)


def _check_segment(kind: str, value: str) -> str:
    if not isinstance(value, str) or not _SEGMENT.match(value) or value in (".", "..") or \
            (kind in ("client", "usecase") and value in RESERVED):
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
        return sorted(p.name for p in path.iterdir()
                      if p.is_dir() and _SEGMENT.match(p.name) and p.name not in RESERVED)

    def releases(self, client: str, usecase: str) -> dict[str, Any] | None:
        """releases.json for a use case, or None when it has none (unmanaged)."""
        path = self.root / client / usecase / RELEASES_FILE
        if not path.is_file():
            return None
        data = _read_json(path)
        if not isinstance(data, dict) or not isinstance(data.get("history", []), list):
            raise ConfigError("config_invalid", f"{client}/{usecase}/{RELEASES_FILE} must be "
                              '{"active": "<version>", "history": [...]}')
        active = data.get("active")
        if active is not None and (not isinstance(active, str) or not _SEMVER.match(active)):
            raise ConfigError("config_invalid", f"{client}/{usecase}/{RELEASES_FILE}: active {active!r} "
                              "is not a version")
        return {"active": active, "rejected": list(data.get("rejected") or []), "history": data.get("history") or []}

    def status_of(self, client: str, usecase: str, version: str) -> str:
        rel = self.releases(client, usecase)
        if rel is None:
            return "unmanaged"
        active = rel.get("active")
        if version in rel.get("rejected", []):
            return "rejected"
        if version == active:
            return "active"
        if active is None or _semver_key(version) > _semver_key(active):
            return "candidate"
        return "retired"

    def versions(self, client: str, usecase: str) -> list[str]:
        vs = [v for v in self._dirs(self.root / client / usecase) if _SEMVER.match(v)]
        return sorted(vs, key=_semver_key)

    def catalogue(self) -> list[dict[str, Any]]:
        out = []
        for client in self._dirs(self.root):
            for usecase in self._dirs(self.root / client):
                versions = self.versions(client, usecase)
                if versions:
                    try:
                        rel = self.releases(client, usecase)
                    except ConfigError:
                        rel = {"active": None, "history": [], "invalid": True}
                    out.append({"client": client, "usecase": usecase, "versions": versions,
                                "latest": versions[-1], "managed": rel is not None,
                                "active": (rel or {}).get("active"),
                                "status": {v: self.status_of(client, usecase, v) if not (rel or {}).get("invalid")
                                           else "unknown" for v in versions}})
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

        rel = self.releases(client, usecase)
        if version and version != "latest":
            by["version"] = "request"
        elif not version and rel is not None:
            if not rel.get("active"):
                raise ConfigError("config_not_released", f"{client}/{usecase} has no released version; "
                                  "release one, or name a version", {"versions": self.versions(client, usecase)},
                                  status=409)
            version, by["version"] = rel["active"], "release"
        elif not version and d.get("version") and (client, usecase) == (d.get("client"), d.get("usecase")):
            version, by["version"] = d["version"], "default"
        else:
            versions = [v for v in self.versions(client, usecase) if v not in (rel or {}).get("rejected", [])]
            if not versions:
                raise ConfigError("config_not_found", f"no versions for {client}/{usecase}",
                                  {"available": self.catalogue()}, status=404)
            version, by["version"] = versions[-1], "latest"
        _check_segment("version", version)

        folder = self.root / client / usecase / version
        if not folder.is_dir():
            raise ConfigError("config_not_found", f"no config at {client}/{usecase}/{version}",
                              {"available": self.catalogue()}, status=404)
        cfg = self._load(folder, client, usecase, version, by)
        status = "unmanaged" if rel is None else ("active" if version == rel.get("active") else
                                                  self.status_of(client, usecase, version))
        if status != cfg.release_status:
            cfg = self._copy(cfg, release_status=status)
        return cfg

    @staticmethod
    def _copy(cfg: ExtractionConfig, **changes: Any) -> ExtractionConfig:
        out = dataclasses.replace(cfg, **changes)
        if "_ingestion_filter" in cfg.__dict__:
            object.__setattr__(out, "_ingestion_filter", cfg.__dict__["_ingestion_filter"])
        return out

    # ---------------------------------------------------------- loading

    def _load(self, folder: Path, client: str, usecase: str, version: str,
              by: dict[str, str]) -> ExtractionConfig:
        from .ingest_filter import fingerprint
        sha = folder_sha256(folder)
        key = (str(folder), sha, fingerprint(self.root, client, usecase))
        with self._lock:
            cached = self._cache.get(key)
        if cached:
            return self._copy(cached, resolved_by=dict(by))

        manifest = _read_json(folder / "manifest.json")
        if not isinstance(manifest, dict):
            raise ConfigError("config_invalid", "manifest.json must be an object")
        prompt_path = folder / "prompts" / "system.md"
        if not prompt_path.is_file():
            raise ConfigError("config_incomplete", "missing prompts/system.md", {"path": str(folder)})
        provider = (manifest.get("model") or {}).get("provider", "stub")
        if provider not in ("stub", "gateway"):
            raise ConfigError("config_invalid", f"model.provider must be 'stub' or 'gateway', got {provider!r}")

        loaded: dict[str, Skill] = {}

        def skills_of(names: Any, where: str) -> list[Skill]:
            if not isinstance(names, list):
                raise ConfigError("config_invalid", f"{where} must be a list of skill names")
            out = []
            for name in names:
                _check_segment("skill", name)
                if name not in loaded:
                    p = folder / "skills" / f"{name}.md"
                    if not p.is_file():
                        raise ConfigError("config_incomplete",
                                          f"skill {name!r} listed but skills/{name}.md missing")
                    loaded[name] = parse_skill(name, p.read_text(encoding="utf-8"))
                out.append(loaded[name])
            return out

        shared = skills_of(manifest.get("skills", []), "manifest skills")
        types: dict[str, EmailType] = {}
        declared = manifest.get("types")
        if declared is not None:
            if not isinstance(declared, dict) or not declared:
                raise ConfigError("config_invalid", "manifest types must be a non-empty object")
            for tname, spec in declared.items():
                _check_segment("email type", tname)
                if not isinstance(spec, dict):
                    raise ConfigError("config_invalid", f"manifest types.{tname} must be an object")
                rel = spec.get("schema", "schema.json")
                path = (folder / rel).resolve()
                if folder.resolve() not in path.parents:
                    raise ConfigError("config_invalid", f"types.{tname}.schema must stay inside the version folder")
                base = load_dictionary(_read_json(path))
                tskills = tuple(shared + skills_of(spec.get("skills", []), f"types.{tname}.skills"))
                accept = (spec.get("thresholds") or {}).get("accept_at")
                types[tname] = EmailType(tname, apply_hints(base, [(s.name, s.hints) for s in tskills]),
                                         base, tskills, float(accept) if accept is not None else None)
        detection = None
        if (folder / DETECTION_FILE).is_file():
            from .classify import validate
            detection = validate(_read_json(folder / DETECTION_FILE))

        if types:
            first = types[manifest["default_type"]] if manifest.get("default_type") in types \
                else next(iter(types.values()))
            dictionary, base_dictionary, skills, accept = first.dictionary, first.base_dictionary, \
                first.skills, first.accept_at
        else:
            base_dictionary = load_dictionary(_read_json(folder / "schema.json"))
            dictionary = apply_hints(base_dictionary, [(s.name, s.hints) for s in shared])
            skills, accept = tuple(shared), None
        cfg = ExtractionConfig(client=client, usecase=usecase, version=version, path=folder,
                               sha256=sha, manifest=manifest, dictionary=dictionary,
                               system_prompt=prompt_path.read_text(encoding="utf-8"),
                               skills=skills, resolved_by=dict(by), base_dictionary=base_dictionary,
                               root=self.root, types=types, detection=detection, type_accept_at=accept)
        cfg.ingestion_filter                      # load and validate lookup data now, not mid-run
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
