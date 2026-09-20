"""Registry routes: publish, inspect, sign off, promote, roll back."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field

from ...contracts.artifacts import PackageManifest
from ...registry.errors import RegistryError
from ...registry.repository import Registry
from ..deps import as_http, registry_dep, settings_dep

router = APIRouter(prefix="/registry", tags=["registry"])


class PublishRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    manifest: PackageManifest
    artifacts: dict[str, Any] = Field(default_factory=dict)
    bodies: dict[str, str] = Field(default_factory=dict)
    eval_report: dict[str, Any] | None = None
    gate_failed: bool = False


class PackageSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

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


class SignoffRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    identity: str
    note: str | None = None


class PromoteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    promoted_by: str


@router.post("/packages", response_model=PackageSummary, status_code=201, summary="Publish an immutable package version (CTR-01..05, CTR-17)")
def publish(payload: PublishRequest, registry: Registry = Depends(registry_dep)):
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
    return _summary(pkg)


@router.get("/packages", response_model=list[PackageSummary], summary="List packages")
def list_packages(
    client_id: str | None = Query(default=None),
    workflow_id: str | None = Query(default=None),
    registry: Registry = Depends(registry_dep),
):
    return [_summary(p) for p in registry.list_packages(client_id, workflow_id)]


@router.get(
    "/packages/{client_id}/{workflow_id}/{version}",
    response_model=PackageSummary,
    summary="Load a package, verifying every checksum (CTR-02)",
)
def get_package(client_id: str, workflow_id: str, version: str, registry: Registry = Depends(registry_dep)):
    try:
        return _summary(registry.get(client_id, workflow_id, version))
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
)
def sign_off(
    client_id: str,
    workflow_id: str,
    version: str,
    payload: SignoffRequest,
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
)
def promote(
    client_id: str,
    workflow_id: str,
    version: str,
    payload: PromoteRequest,
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
)
def rollback(
    client_id: str,
    workflow_id: str,
    payload: PromoteRequest,
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
    response_model=PackageSummary | None,
    summary="The one active version in production (CTR-20)",
)
def active(client_id: str, workflow_id: str, registry: Registry = Depends(registry_dep)):
    pkg = registry.active(client_id, workflow_id)
    return _summary(pkg) if pkg else None


@router.get(
    "/workflows/{client_id}/{workflow_id}/resolve",
    response_model=PackageSummary,
    summary="What runtime resolves at run start, engine-checked (RT-01, CTR-03)",
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
    return _summary(pkg)


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
