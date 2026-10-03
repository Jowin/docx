"""Registry routes: publish, inspect, sign off, promote, roll back."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from fastapi import APIRouter, Depends, Query

from ...records import Record
from ...contracts.artifacts import PackageManifest
from ...registry.errors import RegistryError
from ...registry.repository import Registry
from ..deps import as_http, registry_dep, settings_dep
from ..typed import docs, json_body, out

router = APIRouter(prefix="/registry", tags=["registry"])


@dataclass(kw_only=True)
class PublishRequest(Record):
    manifest: PackageManifest
    artifacts: dict[str, Any] = field(default_factory=dict)
    bodies: dict[str, str] = field(default_factory=dict)
    eval_report: dict[str, Any] | None = None
    gate_failed: bool = False


@dataclass(kw_only=True)
class PackageSummary(Record):
    client_id: str
    workflow_id: str
    version: str
    state: str
    engine_range: str
    email_types: list[str]
    gate_failed: bool
    manifest_sha256: str
    artifact_count: int
    signoff_count: int
    promotion_count: int
    created_by: str
    source_corpus_id: str


def _summary(pkg) -> PackageSummary:
    return PackageSummary(
        client_id=pkg.client_id,
        workflow_id=pkg.workflow_id,
        version=pkg.version,
        state=pkg.state.value,
        engine_range=pkg.engine_range,
        email_types=list(pkg.email_types),
        gate_failed=pkg.gate_failed,
        manifest_sha256=pkg.manifest_sha256,
        artifact_count=len(pkg.artifacts),
        signoff_count=len(pkg.signoffs),
        promotion_count=len(pkg.promotions),
        created_by=pkg.created_by,
        source_corpus_id=pkg.source_corpus_id,
    )


@dataclass(kw_only=True)
class SignoffRequest(Record):
    identity: str
    note: str | None = None


@dataclass(kw_only=True)
class PromoteRequest(Record):
    promoted_by: str


@router.post(
    "/packages",
    summary="Publish an immutable package version (CTR-01..05, CTR-17)",
    **docs(PublishRequest, PackageSummary, status=201),
)
def publish(
    payload: PublishRequest = Depends(json_body(PublishRequest)),
    registry: Registry = Depends(registry_dep),
):
    try:
        pkg = registry.publish(
            manifest=payload.manifest,
            artifacts=payload.artifacts,
            bodies=payload.bodies,
            eval_report=payload.eval_report,
            gate_failed=payload.gate_failed,
        )
    except RegistryError as exc:
        raise as_http(exc) from exc
    return out(_summary(pkg))


@router.get("/packages", summary="List packages", **docs(response=PackageSummary, many=True))
def list_packages(
    client_id: str | None = Query(default=None),
    workflow_id: str | None = Query(default=None),
    registry: Registry = Depends(registry_dep),
):
    return out([_summary(p) for p in registry.list_packages(client_id, workflow_id)])


@router.get(
    "/packages/{client_id}/{workflow_id}/{version}",
    summary="Load a package, verifying every checksum (CTR-02)",
    **docs(response=PackageSummary),
)
def get_package(client_id: str, workflow_id: str, version: str, registry: Registry = Depends(registry_dep)):
    try:
        return out(_summary(registry.get(client_id, workflow_id, version)))
    except RegistryError as exc:
        raise as_http(exc) from exc


@router.get(
    "/packages/{client_id}/{workflow_id}/{version}/artifacts",
    summary="Artifact contents by path",
)
def get_artifacts(client_id: str, workflow_id: str, version: str, registry: Registry = Depends(registry_dep)):
    try:
        pkg = registry.get(client_id, workflow_id, version)
    except RegistryError as exc:
        raise as_http(exc) from exc
    return registry.artifact_contents(pkg)


@router.post(
    "/packages/{client_id}/{workflow_id}/{version}/signoff",
    summary="Record an immutable human sign-off (DT-33)",
    **docs(SignoffRequest),
)
def sign_off(
    client_id: str,
    workflow_id: str,
    version: str,
    payload: SignoffRequest = Depends(json_body(SignoffRequest)),
    registry: Registry = Depends(registry_dep),
):
    try:
        pkg = registry.get(client_id, workflow_id, version)
        row = registry.sign_off(pkg, payload.identity, payload.note)
    except RegistryError as exc:
        raise as_http(exc) from exc
    return {
        "package": pkg.coordinate,
        "identity": row.identity,
        "signed_at": row.signed_at,
        "eval_report_sha256": row.eval_report_sha256,
    }


@router.post(
    "/packages/{client_id}/{workflow_id}/{version}/promote",
    summary="Promote to production; needs a passing report and a sign-off (CTR-19)",
    **docs(PromoteRequest),
)
def promote(
    client_id: str,
    workflow_id: str,
    version: str,
    payload: PromoteRequest = Depends(json_body(PromoteRequest)),
    registry: Registry = Depends(registry_dep),
):
    try:
        pkg = registry.get(client_id, workflow_id, version)
        row = registry.promote(pkg, payload.promoted_by)
    except RegistryError as exc:
        raise as_http(exc) from exc
    return {
        "package": pkg.coordinate,
        "promoted_by": row.promoted_by,
        "promoted_at": row.promoted_at,
        "manifest_sha256": row.manifest_sha256,
        "state": pkg.state.value,
    }


@router.post(
    "/workflows/{client_id}/{workflow_id}/rollback",
    summary="Point activation back at the previous promoted version (CTR-20)",
    **docs(PromoteRequest),
)
def rollback(
    client_id: str,
    workflow_id: str,
    payload: PromoteRequest = Depends(json_body(PromoteRequest)),
    registry: Registry = Depends(registry_dep),
):
    try:
        row = registry.rollback(client_id, workflow_id, payload.promoted_by)
    except RegistryError as exc:
        raise as_http(exc) from exc
    active = registry.active(client_id, workflow_id)
    return {"active": active.coordinate if active else None, "rolled_back_at": row.promoted_at}


@router.get(
    "/workflows/{client_id}/{workflow_id}/active",
    summary="The one active version in production (CTR-20)",
    **docs(response=PackageSummary),
)
def active(client_id: str, workflow_id: str, registry: Registry = Depends(registry_dep)):
    pkg = registry.active(client_id, workflow_id)
    return out(_summary(pkg)) if pkg else None


@router.get(
    "/workflows/{client_id}/{workflow_id}/resolve",
    summary="What runtime resolves at run start, engine-checked (RT-01, CTR-03)",
    **docs(response=PackageSummary),
)
def resolve(
    client_id: str,
    workflow_id: str,
    engine_version: str | None = Query(default=None),
    registry: Registry = Depends(registry_dep),
    settings=Depends(settings_dep),
):
    try:
        pkg = registry.resolve_for_engine(
            client_id, workflow_id, engine_version or settings.engine_version
        )
    except RegistryError as exc:
        raise as_http(exc) from exc
    return out(_summary(pkg))


@router.get(
    "/packages/{client_id}/{workflow_id}/{version}/verify",
    summary="Recompute checksums and re-check the promotion (CTR-02, CTR-18)",
)
def verify(client_id: str, workflow_id: str, version: str, registry: Registry = Depends(registry_dep)):
    try:
        pkg = registry.get(client_id, workflow_id, version)
        registry.verify_checksums(pkg)
    except RegistryError as exc:
        raise as_http(exc) from exc
    return {
        "package": pkg.coordinate,
        "checksums_ok": True,
        "promotion_bytes_match": registry.verify_promotion(pkg),
    }
