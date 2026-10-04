"""Turn authoring artifacts into a runtime config version folder.

The authoring run's artifacts (field schemas per email type, detection rules,
thresholds, authored skills) are written in the runtime's own layout
(runtime ``config_store``), so the folder the evaluation tests is byte for
byte the folder that is published and, once released, served::

    manifest.json            types, default_type, thresholds per type, model, evidence, ...
    schema.json              the first type's dictionary
    schemas/<type>.json      every other type's dictionary
    rules/detection.json     the detection rules, as CTR-08 writes them
    prompts/system.md        from the base version (or the template config)
    skills/<name>.md         base skills, learned patterns carried over, authored overrides

Operational settings (model, evidence budgets, intake limits, locale, output
format, cost limits, classification) and the system prompt come from the
*base*: the use case's latest version when there is one, otherwise the
template config (``defaults.json``'s client and use case; a template lends
only its operational settings, not its prompt or skills). Skills learned by
pattern learning (``kind: learned-pattern``) are carried into the new version
as shared skills, so a redesign keeps what was learned one sample at a time.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any

import yaml

from . import configroot
from .contracts.artifacts import DetectionRules, FieldSchema, Thresholds

LEARNED_KIND = "learned-pattern"
AUTHORED_KIND = "authored"
_KEEP = ("model", "evidence", "intake", "locale", "output", "limits", "concurrency", "classification")
_FRONT = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.S)
FALLBACK_PROMPT = (
    "You extract structured records from business emails and their attachments.\n"
    "Use only values present in the evidence; cite the locator of every value.\n"
    "Return null for a field the evidence does not contain.\n"
)


def _slug(text: str) -> str:
    s = re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-._") or "skill"
    return s[:64]


def runtime_schema(schema: FieldSchema) -> dict[str, Any]:
    """CTR-07 field schema -> the runtime's data dictionary (schema.json)."""
    fields = []
    for f, required in [(f, True) for f in schema.required_fields] + [(f, False) for f in schema.optional_fields]:
        entry: dict[str, Any] = {"name": f.name, "type": f.type}
        if required:
            entry["required"] = True
        if f.critical:
            entry["critical"] = True
        labels = [f.name.replace("_", " ")] + [a for a in schema.aliases.get(f.name, [])]
        seen: list[str] = []
        for a in labels:
            a = " ".join(str(a).split()).lower()
            if a and a not in seen:
                seen.append(a)
        entry["aliases"] = seen
        fields.append(entry)
    return {"name": schema.email_type, "description": f"{schema.email_type} records, authored from a corpus.",
            "fields": fields}


def base_folder(root: Path, client: str, usecase: str) -> tuple[Path | None, str | None]:
    """The version a new authored version inherits settings from: this use case's latest, else the template."""
    v = configroot.latest(root, client, usecase)
    if v:
        return Path(root) / client / usecase / v, v
    d = configroot.defaults(root)
    if d.get("client") and d.get("usecase"):
        tv = configroot.served_version(root, d["client"], d["usecase"])
        if tv:
            return Path(root) / d["client"] / d["usecase"] / tv, None
    return None, None


def _skill_meta(path: Path) -> dict[str, Any]:
    m = _FRONT.match(path.read_text(encoding="utf-8"))
    if not m:
        return {}
    meta = yaml.safe_load(m.group(1)) or {}
    return meta if isinstance(meta, dict) else {}


def _skill_file(name: str, body: str, meta: dict[str, Any]) -> str:
    return "---\n" + yaml.safe_dump({"name": name, **meta}, sort_keys=False).strip() + "\n---\n" + body.strip() + "\n"


def write_version(dst: Path, *, client: str, usecase: str, schemas: dict[str, FieldSchema],
                  detection: DetectionRules | None, thresholds: Thresholds | None,
                  skills: list[Any] | None = None, base: Path | None = None,
                  description: str | None = None, default_type: str | None = None,
                  version: str = "0.0.0") -> Path:
    """Write a complete config version folder at ``dst`` (replacing it). ``skills`` are SkillBundles."""
    if not schemas:
        raise configroot.ConfigRootError("an authored config needs at least one email type", code="no_types",
                                         status=422)
    dst = Path(dst)
    if dst.exists():
        shutil.rmtree(dst)
    (dst / "prompts").mkdir(parents=True)
    (dst / "skills").mkdir()
    base_manifest: dict[str, Any] = {}
    if base is not None and (base / "manifest.json").is_file():
        base_manifest = json.loads((base / "manifest.json").read_text(encoding="utf-8"))
    # a template from another use case lends its settings, not its domain prompt or skills
    same = base is not None and (base.parent.parent.name, base.parent.name) == (client, usecase)
    prompt = base / "prompts" / "system.md" if same else None
    (dst / "prompts" / "system.md").write_text(
        prompt.read_text(encoding="utf-8") if prompt is not None and prompt.is_file() else FALLBACK_PROMPT,
        encoding="utf-8")

    # shared skills: the base's general skills, and every learned pattern
    shared: list[str] = []
    base_skill_names = list(base_manifest.get("skills") or [])
    for spec in (base_manifest.get("types") or {}).values():
        base_skill_names += [s for s in spec.get("skills", []) if s not in base_skill_names]
    for name in base_skill_names if same else []:
        src = base / "skills" / f"{name}.md" if base is not None else None
        if src is None or not src.is_file():
            continue
        meta = _skill_meta(src)
        if meta.get("kind") == AUTHORED_KIND:
            continue                     # authored for an earlier design; re-authored below
        shutil.copy2(src, dst / "skills" / src.name)
        if name not in shared:
            shared.append(name)

    per_type: dict[str, list[str]] = {t: [] for t in schemas}
    for bundle in skills or []:
        m = bundle.manifest
        etype = (m.inputs or {}).get("email_type") if isinstance(m.inputs, dict) else None
        etype = etype if etype in schemas else None
        name = _slug(f"{m.skill_id}" if etype is None else f"{etype}-{m.skill_id}")
        meta = {"kind": AUTHORED_KIND, "skill_id": m.skill_id, "bound_agents": list(m.bound_agents)}
        if etype:
            meta["email_type"] = etype
        (dst / "skills" / f"{name}.md").write_text(_skill_file(name, bundle.body, meta), encoding="utf-8")
        for t in ([etype] if etype else list(schemas)):
            if name not in per_type[t]:
                per_type[t].append(name)

    types: dict[str, Any] = {}
    order = [default_type] + [t for t in schemas if t != default_type] if default_type in schemas else list(schemas)
    for i, t in enumerate(order):
        rel = "schema.json" if i == 0 else f"schemas/{t}.json"
        path = dst / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(runtime_schema(schemas[t]), indent=2) + "\n", encoding="utf-8")
        spec: dict[str, Any] = {"schema": rel, "skills": per_type[t]}
        tt = (thresholds.types.get(t) if thresholds else None)
        if tt is not None:
            spec["thresholds"] = {"accept_at": tt.accept_at}
        types[t] = spec

    if detection is not None and detection.types:
        (dst / "rules").mkdir()
        (dst / "rules" / "detection.json").write_text(json.dumps(detection.to_dict(), indent=2, default=str) + "\n",
                                                     encoding="utf-8")

    manifest: dict[str, Any] = {"client": client, "usecase": usecase, "version": version,
                                "description": description or base_manifest.get("description")
                                or f"{client} {usecase}: authored from a corpus.",
                                **{k: base_manifest[k] for k in _KEEP if k in base_manifest},
                                "skills": shared, "default_type": order[0], "types": types}
    manifest.setdefault("model", {"provider": "stub", "name": "deterministic-stub", "max_tokens": 4096})
    learned = [n for n in shared if _skill_meta(dst / "skills" / f"{n}.md").get("kind") == LEARNED_KIND]
    if learned:
        manifest["learned_patterns"] = learned
    (dst / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return dst


def set_thresholds(folder: Path, thresholds: Thresholds) -> None:
    """Rewrite each type's accept threshold in a written version folder."""
    path = Path(folder) / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    for t, spec in (manifest.get("types") or {}).items():
        tt = thresholds.types.get(t)
        if tt is not None:
            spec["thresholds"] = {"accept_at": tt.accept_at}
    path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
