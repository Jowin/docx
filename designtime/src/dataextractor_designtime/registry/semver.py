"""Semver rules for package versions (CTR-17).

    patch  threshold or alias change only
    minor  new field, new skill, or new email type
    major  a required field removed or renamed, or an email type removed

The packager computes the required bump from the artifact diff and the registry
refuses a published version that understates it.
"""

from __future__ import annotations

import re
from typing import Any, Literal

Bump = Literal["none", "patch", "minor", "major"]
_ORDER: dict[Bump, int] = {"none": 0, "patch": 1, "minor": 2, "major": 3}
_SEMVER_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")


def parse(version: str) -> tuple[int, int, int]:
    m = _SEMVER_RE.match(version)
    if not m:
        raise ValueError(f"not a semver version: {version!r}")
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


def observed_bump(previous: str, new: str) -> Bump:
    """What the version numbers themselves claim changed."""
    pm, pn, pp = parse(previous)
    nm, nn, np_ = parse(new)
    if (nm, nn, np_) <= (pm, pn, pp):
        raise ValueError(f"version {new} does not advance {previous}")
    if nm > pm:
        return "major"
    if nn > pn:
        return "minor"
    return "patch"


def _schemas_by_type(artifacts: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        content["email_type"]: content
        for path, content in artifacts.items()
        if path.startswith("schemas/") and isinstance(content, dict) and "email_type" in content
    }


def _skill_ids(artifacts: dict[str, Any]) -> set[str]:
    return {
        content["skill_id"]
        for path, content in artifacts.items()
        if path.startswith("skills/") and isinstance(content, dict) and "skill_id" in content
    }


def required_bump(previous: dict[str, Any] | None, new: dict[str, Any]) -> Bump:
    """Smallest bump the diff justifies.

    ``previous`` and ``new`` map artifact path -> parsed content. A package with
    no predecessor requires no bump; it is the first version.
    """
    if previous is None:
        return "none"

    prev_schemas = _schemas_by_type(previous)
    new_schemas = _schemas_by_type(new)

    # An email type disappearing breaks every consumer of it.
    if set(prev_schemas) - set(new_schemas):
        return "major"

    for email_type, prev_schema in prev_schemas.items():
        new_schema = new_schemas[email_type]
        prev_required = {f["name"] for f in prev_schema.get("required_fields", [])}
        new_required = {f["name"] for f in new_schema.get("required_fields", [])}
        # A removal and a rename look the same from outside: a name is gone.
        if prev_required - new_required:
            return "major"

    if set(new_schemas) - set(prev_schemas):
        return "minor"
    if _skill_ids(new) - _skill_ids(previous):
        return "minor"
    for email_type, prev_schema in prev_schemas.items():
        new_schema = new_schemas[email_type]
        prev_fields = {f["name"] for f in prev_schema.get("required_fields", [])} | {
            f["name"] for f in prev_schema.get("optional_fields", [])
        }
        new_fields = {f["name"] for f in new_schema.get("required_fields", [])} | {
            f["name"] for f in new_schema.get("optional_fields", [])
        }
        if new_fields - prev_fields:
            return "minor"

    return "patch" if previous != new else "none"


def is_sufficient(declared: Bump, required: Bump) -> bool:
    return _ORDER[declared] >= _ORDER[required]


_COMPARATOR_RE = re.compile(r"^(>=|<=|>|<|==|=)?\s*(\d+\.\d+\.\d+)$")


def range_matches(range_expr: str, version: str) -> bool:
    """Whitespace-separated comparators, all of which must hold (CTR-03).

    Example: ">=2.1.0 <3.0.0".
    """
    target = parse(version)
    for token in range_expr.split():
        m = _COMPARATOR_RE.match(token.strip())
        if not m:
            raise ValueError(f"unparsable comparator in engine_range: {token!r}")
        op, bound_s = m.group(1) or "==", m.group(2)
        bound = parse(bound_s)
        if op == ">=" and not target >= bound:
            return False
        if op == ">" and not target > bound:
            return False
        if op == "<=" and not target <= bound:
            return False
        if op == "<" and not target < bound:
            return False
        if op in {"==", "="} and target != bound:
            return False
    return True
