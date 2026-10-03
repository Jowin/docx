"""Config-folder operations for learning: scratch copies, candidate versions, publication.

Folders follow the runtime's layout (``<root>/<client>/<usecase>/<version>/``
with ``manifest.json``, ``schema.json``, ``prompts/`` and ``skills/``). A
published version is never modified: learning always writes a new patch
version next to its base (1.0.0 -> 1.0.1, or the next free patch).
"""

from __future__ import annotations

import json
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any

import yaml

from ..agents.base import AgentError

_SEMVER = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
_FRONT = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.S)
LEARNED_KIND = "learned-pattern"


class SkillConflict(AgentError):
    code = "skill_conflict"
    status = 409


def _ignore(_dir: str, names: list[str]) -> list[str]:
    return [n for n in names if n.startswith(".") or n == "__pycache__"]


def scratch_into(config_root: Path, folder: Path) -> Path:
    """A private copy of every config under ``folder``, kept until the learning call finishes."""
    if not Path(config_root).is_dir():
        raise AgentError(f"no runtime configs at {config_root} (set RUNTIME_CONFIG_ROOT)",
                         code="config_root_missing", status=500)
    dst = Path(folder) / "configs"
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(config_root, dst, ignore=_ignore)
    return dst


def scratch_copy(config_root: Path) -> tuple[tempfile.TemporaryDirectory, Path]:
    """A private copy of every config, for one learning call."""
    if not Path(config_root).is_dir():
        raise AgentError(f"no runtime configs at {config_root} (set RUNTIME_CONFIG_ROOT)",
                         code="config_root_missing", status=500)
    tmp = tempfile.TemporaryDirectory(prefix="dt-learn-")
    dst = Path(tmp.name) / "configs"
    shutil.copytree(config_root, dst, ignore=_ignore)
    return tmp, dst


def versions(root: Path, client: str, usecase: str) -> list[str]:
    folder = Path(root) / client / usecase
    if not folder.is_dir():
        return []
    return sorted((p.name for p in folder.iterdir() if p.is_dir() and _SEMVER.match(p.name)),
                  key=lambda v: tuple(int(x) for x in v.split(".")))


def next_patch(base: str, *taken: list[str]) -> str:
    """The next free patch version on the base's major.minor line."""
    m = _SEMVER.match(base)
    if not m:
        raise AgentError(f"base version {base!r} is not semantic", code="version_invalid")
    major, minor, patch = (int(x) for x in m.groups())
    used = {v for t in taken for v in t}
    patch += 1
    while f"{major}.{minor}.{patch}" in used:
        patch += 1
    return f"{major}.{minor}.{patch}"


def read_skill(folder: Path, name: str) -> tuple[dict[str, Any], str] | None:
    """(front matter, body) of skills/<name>.md, or None when there is no such skill."""
    path = Path(folder) / "skills" / f"{name}.md"
    if not path.is_file():
        return None
    text = path.read_text(encoding="utf-8")
    m = _FRONT.match(text)
    if not m:
        return {}, text
    meta = yaml.safe_load(m.group(1)) or {}
    return (meta if isinstance(meta, dict) else {}), text[m.end():]


def existing_meta(folder: Path, pattern: str) -> dict[str, Any]:
    """Front matter of this pattern's learned skill in ``folder``; refuses a hand-written skill."""
    found = read_skill(folder, pattern)
    if found is None:
        return {}
    meta, _ = found
    if meta.get("kind") != LEARNED_KIND:
        raise SkillConflict(f"skills/{pattern}.md exists and was not learned; pick another pattern_name",
                            detail={"pattern_name": pattern})
    return meta


def existing_hints(folder: Path, pattern: str) -> dict[str, Any]:
    """Hints of this pattern's learned skill in ``folder``; refuses to overwrite a hand-written skill."""
    found = read_skill(folder, pattern)
    if found is None:
        return {}
    meta, _ = found
    if meta.get("kind") != LEARNED_KIND:
        raise SkillConflict(f"skills/{pattern}.md exists and was not learned; pick another pattern_name",
                            detail={"pattern_name": pattern})
    return meta.get("hints") or {}


def write_candidate(root: Path, client: str, usecase: str, base: str, version: str,
                    pattern: str, markdown: str) -> Path:
    """Copy the base version to ``version`` under ``root`` and add the pattern's skill."""
    src = Path(root) / client / usecase / base
    dst = Path(root) / client / usecase / version
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst, ignore=_ignore)
    (dst / "skills").mkdir(exist_ok=True)
    (dst / "skills" / f"{pattern}.md").write_text(markdown, encoding="utf-8")
    manifest_path = dst / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    skills = list(manifest.get("skills") or [])
    if pattern not in skills:
        skills.append(pattern)
    manifest["skills"] = skills
    if "version" in manifest:
        manifest["version"] = version
    learned = [p for p in manifest.get("learned_patterns", []) if p != pattern]
    manifest["learned_patterns"] = learned + [pattern]
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return dst


def publish(candidate: Path, root: Path, client: str, usecase: str, base: str) -> str:
    """Copy a tested candidate into the live root as the next free patch; returns its version.

    The version folder is created exclusively, so two learning calls racing for
    the same number get different versions rather than one overwriting the other.
    """
    parent = Path(root) / client / usecase
    tried: list[str] = []
    for _ in range(50):
        version = next_patch(base, versions(root, client, usecase), tried)
        dst = parent / version
        try:
            dst.mkdir(parents=False)
        except FileExistsError:
            tried.append(version)
            continue
        try:
            for item in candidate.iterdir():
                if item.is_dir():
                    shutil.copytree(item, dst / item.name, ignore=_ignore)
                else:
                    shutil.copy2(item, dst / item.name)
            manifest_path = dst / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if "version" in manifest:
                manifest["version"] = version
                manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        except Exception:
            shutil.rmtree(dst, ignore_errors=True)
            raise
        return version
    raise AgentError("no free version to publish to", code="version_exhausted", status=409)
