"""The registry against a live Postgres: immutability, checksums, promotion."""

from __future__ import annotations

import pytest

from dataextractor_designtime.agents import (
    CorpusProfiler,
    DetectionRulesAgent,
    DetectionRulesInput,
    FieldSchemaAgent,
    FieldSchemaInput,
    Packager,
    PackagerInput,
    ProfilerInput,
)
from dataextractor_designtime.contracts.artifacts import Thresholds, TypeThresholds, sha256_of
from dataextractor_designtime.registry.errors import (
    EngineIncompatible,
    GateFailed,
    PackageCorrupt,
    PackageNotFound,
    SignoffRequired,
    VersionExists,
)
from dataextractor_designtime.registry.models import PackageState

PASSING_REPORT = {"metrics": {"field_accuracy": 0.97}, "gate_results": {"field_accuracy": True}}
FAILING_REPORT = {"metrics": {"field_accuracy": 0.61}, "gate_results": {"field_accuracy": False}}


@pytest.fixture()
def package_parts(corpus, corpus_root):
    profile = CorpusProfiler().run(ProfilerInput(corpus=corpus, corpus_root=str(corpus_root)))
    schema = FieldSchemaAgent().run(
        FieldSchemaInput(
            corpus=corpus,
            email_type="invoice",
            observed_column_labels=profile.observed_column_labels,
            confirmed_critical=["amount", "due_date"],
        )
    ).artifact
    detection = DetectionRulesAgent().run(
        DetectionRulesInput(corpus=corpus, email_types=["invoice"])
    ).artifact
    thresholds = Thresholds(types={"invoice": TypeThresholds()}, generated_by="test")
    return schema, detection, thresholds


def _build(package_parts, version="1.0.0", report=PASSING_REPORT, reviewed_by="joe@acme.example"):
    schema, detection, thresholds = package_parts
    return Packager().run(
        PackagerInput(
            client_id="acme",
            workflow_id="ap-invoices",
            source_corpus_id="acme/corpus/2026-09-01",
            schemas={"invoice": schema},
            detection=detection,
            thresholds=thresholds,
            eval_report=report,
            version=version,
            reviewed_by=reviewed_by,
        )
    )


def _publish(registry, built, report=PASSING_REPORT):
    return registry.publish(
        manifest=built.manifest,
        artifacts=built.artifacts,
        bodies=built.bodies,
        eval_report=report,
        gate_failed=built.gate_failed,
    )


def test_publish_then_load_verifies_every_checksum(registry, package_parts):
    built = _build(package_parts)
    pkg = _publish(registry, built)
    assert pkg.state is PackageState.PUBLISHED
    loaded = registry.get("acme", "ap-invoices", "1.0.0")
    assert loaded.coordinate == "acme/ap-invoices@1.0.0"
    assert len(loaded.artifacts) == len(built.manifest.artifacts)


def test_republishing_a_version_is_refused(registry, package_parts):
    built = _build(package_parts)
    _publish(registry, built)
    with pytest.raises(VersionExists):
        _publish(registry, built)


def test_tampering_with_an_artifact_fails_the_load(registry, db_session, package_parts):
    built = _build(package_parts)
    pkg = _publish(registry, built)
    artifact = next(a for a in pkg.artifacts if a.path == "thresholds.json")
    tampered = dict(artifact.content)
    tampered["types"]["invoice"]["accept_at"] = 0.01
    artifact.content = tampered
    db_session.flush()
    with pytest.raises(PackageCorrupt):
        registry.get("acme", "ap-invoices", "1.0.0")


def test_manifest_listing_an_absent_artifact_is_refused(registry, package_parts):
    built = _build(package_parts)
    artifacts = dict(built.artifacts)
    artifacts.pop("thresholds.json")
    with pytest.raises(PackageCorrupt):
        registry.publish(manifest=built.manifest, artifacts=artifacts, bodies=built.bodies)


def test_a_declared_type_without_thresholds_is_refused(registry, package_parts):
    built = _build(package_parts)
    artifacts = dict(built.artifacts)
    artifacts["thresholds.json"] = {"schema_version": "1.0", "types": {}, "generated_by": "t"}
    manifest = built.manifest.model_copy(deep=True)
    for entry in manifest.artifacts:
        if entry.path == "thresholds.json":
            entry.sha256 = sha256_of(artifacts["thresholds.json"])
    from dataextractor_designtime.registry.errors import TypeIncomplete

    with pytest.raises(TypeIncomplete):
        registry.publish(manifest=manifest, artifacts=artifacts, bodies=built.bodies)


def test_promotion_needs_a_signoff(registry, package_parts):
    built = _build(package_parts)
    pkg = _publish(registry, built)
    with pytest.raises(SignoffRequired):
        registry.promote(pkg, "joe@acme.example")

    registry.sign_off(pkg, "joe@acme.example")
    promotion = registry.promote(pkg, "joe@acme.example")
    assert pkg.state is PackageState.PROMOTED
    assert promotion.manifest_sha256 == pkg.manifest_sha256
    assert registry.active("acme", "ap-invoices").version == "1.0.0"
    assert registry.verify_promotion(pkg) is True


def test_unreviewed_artifacts_block_signoff(registry, package_parts):
    built = _build(package_parts, reviewed_by=None)
    pkg = _publish(registry, built)
    with pytest.raises(SignoffRequired, match="unreviewed"):
        registry.sign_off(pkg, "joe@acme.example")


def test_a_gate_failed_package_publishes_but_does_not_promote(registry, package_parts):
    built = _build(package_parts, report=FAILING_REPORT)
    assert built.gate_failed is True
    pkg = _publish(registry, built, report=FAILING_REPORT)
    registry.sign_off(pkg, "joe@acme.example")
    with pytest.raises(GateFailed):
        registry.promote(pkg, "joe@acme.example")


def test_activation_keeps_one_active_version_and_rolls_back(registry, package_parts):
    first = _build(package_parts, version="1.0.0")
    pkg1 = _publish(registry, first)
    registry.sign_off(pkg1, "joe@acme.example")
    registry.promote(pkg1, "joe@acme.example")

    schema, detection, thresholds = package_parts
    bumped = thresholds.model_copy(deep=True)
    bumped.types["invoice"].accept_at = 0.9
    second = Packager().run(
        PackagerInput(
            client_id="acme",
            workflow_id="ap-invoices",
            source_corpus_id="acme/corpus/2026-09-01",
            schemas={"invoice": schema},
            detection=detection,
            thresholds=bumped,
            eval_report=PASSING_REPORT,
            version="1.0.1",
            previous_version="1.0.0",
            previous_artifacts=first.artifacts,
            reviewed_by="joe@acme.example",
        )
    )
    assert second.required_bump == "patch"
    pkg2 = _publish(registry, second)
    registry.sign_off(pkg2, "joe@acme.example")
    registry.promote(pkg2, "joe@acme.example")

    assert registry.active("acme", "ap-invoices").version == "1.0.1"
    assert pkg1.state is PackageState.DEPRECATED

    registry.rollback("acme", "ap-invoices", "joe@acme.example")
    assert registry.active("acme", "ap-invoices").version == "1.0.0"
    assert pkg2.state is PackageState.ROLLED_BACK


def test_resolve_refuses_an_incompatible_engine(registry, package_parts):
    built = _build(package_parts)
    pkg = _publish(registry, built)
    registry.sign_off(pkg, "joe@acme.example")
    registry.promote(pkg, "joe@acme.example")

    assert registry.resolve_for_engine("acme", "ap-invoices", "2.1.3").version == "1.0.0"
    with pytest.raises(EngineIncompatible):
        registry.resolve_for_engine("acme", "ap-invoices", "3.4.0")


def test_missing_package_and_missing_rollback_target(registry):
    with pytest.raises(PackageNotFound):
        registry.get("nobody", "nothing", "1.0.0")
    with pytest.raises(PackageNotFound):
        registry.rollback("nobody", "nothing", "joe@acme.example")


def test_artifact_paths_must_stay_inside_the_package(registry, package_parts):
    built = _build(package_parts)
    artifacts = dict(built.artifacts)
    artifacts["../escape.json"] = {"x": 1}
    with pytest.raises(PackageCorrupt, match="CTR-04"):
        registry.publish(manifest=built.manifest, artifacts=artifacts, bodies=built.bodies)
