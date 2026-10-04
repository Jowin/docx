"""The registry's record of config versions: provenance, evaluation, sign-offs and releases.

The folders are the truth (configroot.py); this is the audit trail beside
them, and the place the release rules are enforced:

* a version is recorded when design-time publishes it (or, for a folder
  written by hand, the first time anyone signs it off or releases it);
* a release needs at least one sign-off from someone other than the version's
  creator (CTR-19), a folder that still hashes to what was recorded, and
  passing evaluation gates unless the releaser overrides them with a note
  (DT-34);
* release, rollback and reject rewrite ``releases.json`` (configroot) and are
  mirrored here with who did it and why.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import configroot
from .errors import ConfigTampered, GateFailed, RegistryError, SelfSignoff, SignoffRequired, VersionNotFound
from .models import ConfigReleaseRow, ConfigSignoffRow, ConfigVersionRow


def _files(folder: Path) -> dict[str, int]:
    return {p.relative_to(folder).as_posix(): p.stat().st_size
            for p in sorted(folder.rglob("*")) if p.is_file() and not p.name.startswith(".")}


class ConfigRegistry:
    def __init__(self, session: Session, root: Path) -> None:
        self.session = session
        self.root = Path(root)

    # ------------------------------------------------------------------ reads

    def get(self, client: str, usecase: str, version: str) -> ConfigVersionRow | None:
        return self.session.execute(select(ConfigVersionRow).where(
            ConfigVersionRow.client_id == client, ConfigVersionRow.usecase == usecase,
            ConfigVersionRow.version == version)).scalar_one_or_none()

    def list(self, client: str, usecase: str) -> list[ConfigVersionRow]:
        return list(self.session.execute(select(ConfigVersionRow).where(
            ConfigVersionRow.client_id == client, ConfigVersionRow.usecase == usecase)).scalars())

    def releases(self, client: str, usecase: str, limit: int = 100) -> list[ConfigReleaseRow]:
        return list(self.session.execute(select(ConfigReleaseRow).where(
            ConfigReleaseRow.client_id == client, ConfigReleaseRow.usecase == usecase)
            .order_by(ConfigReleaseRow.created_at.desc()).limit(limit)).scalars())

    # ------------------------------------------------------------------ writes

    def record(self, published: dict[str, Any], *, evaluation: dict[str, Any] | None = None,
               gates_passed: bool | None = None) -> ConfigVersionRow:
        """Record a version design-time just published (configroot.publish_version's result)."""
        folder = Path(published["path"])
        row = ConfigVersionRow(client_id=published["client"], usecase=published["usecase"],
                               version=published["version"], origin=published.get("origin", "manual"),
                               base_version=published.get("base_version"), run_id=published.get("run_id"),
                               sha256=published["sha256"], files=_files(folder), evaluation=evaluation,
                               gates_passed=gates_passed, created_by=published.get("created_by") or "unknown")
        self.session.add(row)
        try:
            self.session.flush()
        except IntegrityError as exc:
            self.session.rollback()
            raise RegistryError(f"{row.client_id}/{row.usecase}/{row.version} is already recorded") from exc
        return row

    def ensure(self, client: str, usecase: str, version: str) -> ConfigVersionRow:
        """The row for a version, recording a hand-written folder (origin ``manual``) on first sight."""
        row = self.get(client, usecase, version)
        if row is not None:
            return row
        folder = self.root / client / usecase / version
        if not folder.is_dir():
            raise VersionNotFound(f"no config {client}/{usecase}/{version}")
        prov = configroot.provenance(self.root, client, usecase, version)
        return self.record({"client": client, "usecase": usecase, "version": version, "path": str(folder),
                            "sha256": configroot.folder_sha256(folder), "origin": prov.get("origin", "manual"),
                            "base_version": prov.get("base_version"), "run_id": prov.get("run_id"),
                            "created_by": prov.get("created_by") or "manual"})

    def sign_off(self, client: str, usecase: str, version: str, identity: str,
                 note: str | None = None) -> ConfigSignoffRow:
        row = self.ensure(client, usecase, version)
        if identity.strip().lower() == (row.created_by or "").strip().lower():
            raise SelfSignoff(f"{identity} produced {version}; someone else must sign it off")
        if any(s.identity == identity for s in row.signoffs):
            raise RegistryError(f"{identity} already signed off {version}")
        s = ConfigSignoffRow(config_version_id=row.id, identity=identity, note=note)
        row.signoffs.append(s)
        self.session.flush()
        return s

    def _verify(self, row: ConfigVersionRow) -> None:
        folder = self.root / row.client_id / row.usecase / row.version
        if not folder.is_dir():
            raise VersionNotFound(f"the folder for {row.version} is gone")
        sha = configroot.folder_sha256(folder)
        if sha != row.sha256:
            raise ConfigTampered(f"{row.client_id}/{row.usecase}/{row.version} changed after it was published",
                                 detail={"recorded": row.sha256, "found": sha})

    def verify(self, client: str, usecase: str, version: str) -> dict[str, Any]:
        row = self.ensure(client, usecase, version)
        try:
            self._verify(row)
            return {"version": version, "intact": True, "sha256": row.sha256}
        except ConfigTampered as exc:
            return {"version": version, "intact": False, **exc.detail}

    def release(self, client: str, usecase: str, version: str, by: str, note: str | None = None,
                accept_gate_failure: bool = False) -> dict[str, Any]:
        row = self.ensure(client, usecase, version)
        self._verify(row)
        if not row.signoffs:
            raise SignoffRequired(f"{version} has no sign-off; sign it off before releasing (CTR-19)")
        if row.gates_passed is False and not (accept_gate_failure and note):
            raise GateFailed(f"{version} failed an evaluation gate; release it only with accept_gate_failure "
                             "and a note saying why", detail={"evaluation": row.evaluation or {}})
        out = configroot.release(self.root, client, usecase, version, by, note)
        self._log(out, note, gate_override=row.gates_passed is False)
        return out

    def rollback(self, client: str, usecase: str, by: str, note: str | None = None,
                 to: str | None = None) -> dict[str, Any]:
        target = to or configroot.previous_release(self.root, client, usecase)
        if not target:
            raise RegistryError(f"{client}/{usecase} has no earlier release to roll back to")
        row = self.ensure(client, usecase, target)
        self._verify(row)
        out = configroot.release(self.root, client, usecase, target, by, note, action="rollback")
        self._log(out, note)
        return out

    def reject(self, client: str, usecase: str, version: str, by: str, note: str | None = None) -> dict[str, Any]:
        self.ensure(client, usecase, version)
        out = configroot.reject(self.root, client, usecase, version, by, note)
        self._log(out, note)
        return out

    def _log(self, out: dict[str, Any], note: str | None, gate_override: bool = False) -> None:
        self.session.add(ConfigReleaseRow(client_id=out["client"], usecase=out["usecase"], action=out["action"],
                                          version=out["version"], previous=out.get("previous"), by=out["by"],
                                          note=note, gate_override=gate_override))
        self.session.flush()
