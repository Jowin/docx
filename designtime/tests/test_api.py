"""Every component over HTTP, one endpoint at a time, then the whole run."""

from __future__ import annotations

import pytest

EXPECTED_AGENT_ROUTES = {
    "corpus-profiler",
    "type-discovery",
    "field-schema",
    "detection-rules",
    "skill-author",
    "threshold-tuner",
    "evaluation",
    "packager",
}


def test_health_reports_the_database(client):
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["database"]["reachable"] is True


def test_every_agent_has_its_own_endpoint(client):
    body = client.get("/components").json()
    names = {a["name"] for a in body["agents"]}
    assert names == EXPECTED_AGENT_ROUTES
    spec = client.get("/openapi.json").json()
    for name in EXPECTED_AGENT_ROUTES:
        assert f"/agents/{name}/run" in spec["paths"]


def test_profiler_endpoint(client, corpus_dict, corpus_root):
    r = client.post(
        "/agents/corpus-profiler/run",
        json={"corpus": corpus_dict, "corpus_root": str(corpus_root)},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["sample_count"] == 40
    assert body["observed_column_labels"] == ["Amount Due", "Invoice No", "Payment Due", "Vendor Name"]


def test_type_discovery_endpoint(client, corpus_dict):
    r = client.post("/agents/type-discovery/run", json={"corpus": corpus_dict})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["requires_confirmation"] is True
    assert body["proposals"][0]["email_type"] == "invoice"


def test_field_schema_endpoint(client, corpus_dict):
    r = client.post(
        "/agents/field-schema/run",
        json={
            "corpus": corpus_dict,
            "email_type": "invoice",
            "observed_column_labels": ["Amount Due", "Payment Due", "Invoice No", "Vendor Name"],
            "confirmed_critical": ["amount"],
        },
    )
    assert r.status_code == 200, r.text
    artifact = r.json()["artifact"]
    assert artifact["aliases"]["amount"] == ["Amount Due"]
    assert [f["name"] for f in artifact["required_fields"] if f["critical"]] == ["amount"]


def test_detection_rules_endpoint(client, corpus_dict):
    r = client.post(
        "/agents/detection-rules/run",
        json={"corpus": corpus_dict, "email_types": ["invoice"], "rule_support_min": 3},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["artifact"]["types"]["invoice"]["keywords"]
    assert body["dropped"]


def test_threshold_tuner_endpoint(client):
    r = client.post(
        "/agents/threshold-tuner/run",
        json={
            "predictions": [
                {"sample_id": "a", "email_type": "invoice", "confidence": 0.95, "critical_correct": True, "all_correct": True},
                {"sample_id": "b", "email_type": "invoice", "confidence": 0.40, "critical_correct": False, "all_correct": False},
            ]
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["tuning"][0]["chosen_band"] >= 0.5


def test_skill_author_endpoint(client):
    schema = {
        "schema_version": "1.0",
        "email_type": "invoice",
        "required_fields": [{"name": "amount", "type": "decimal", "critical": True}],
        "optional_fields": [],
        "aliases": {"amount": ["Amount Due"]},
        "generated_by": "test",
    }
    r = client.post(
        "/agents/skill-author/run",
        json={
            "email_type": "invoice",
            "field_schema": schema,
            "skill_ids": ["field-mapping"],
            "baseline_metrics": {"field-mapping": 0.5},
            "failure_cases": {"field-mapping": ["inv_0001"]},
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["skills"][0]["manifest"]["skill_id"] == "field-mapping"


def test_packager_endpoint_rejects_an_insufficient_bump(client, corpus_dict):
    schema = client.post(
        "/agents/field-schema/run",
        json={"corpus": corpus_dict, "email_type": "invoice", "confirmed_critical": []},
    ).json()["artifact"]
    detection = client.post(
        "/agents/detection-rules/run", json={"corpus": corpus_dict, "email_types": ["invoice"]}
    ).json()["artifact"]
    thresholds = {"schema_version": "1.0", "types": {"invoice": {}}, "generated_by": "t"}
    previous = {
        "schemas/invoice.json": {
            "email_type": "invoice",
            "required_fields": [{"name": n} for n in ["amount", "due_date", "invoice_number", "vendor", "gone"]],
            "optional_fields": [],
        }
    }
    r = client.post(
        "/agents/packager/run",
        json={
            "client_id": "acme",
            "workflow_id": "ap-invoices",
            "source_corpus_id": "c",
            "schemas": {"invoice": schema},
            "detection": detection,
            "thresholds": thresholds,
            "version": "1.0.1",
            "previous_version": "1.0.0",
            "previous_artifacts": previous,
        },
    )
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "version_bump_insufficient"


def test_authoring_run_publishes_and_promotes(client, corpus_dict, corpus_root):
    run = client.post(
        "/runs",
        json={
            "corpus": corpus_dict,
            "corpus_root": str(corpus_root),
            "client_id": "acme",
            "workflow_id": "ap-invoices",
            "confirmed_types": ["invoice"],
            "confirmed_critical": {"invoice": ["amount", "due_date"]},
            "reviewed_by": "joe@acme.example",
            "publish": True,
        },
    )
    assert run.status_code == 200, run.text
    body = run.json()
    assert body["published"] is True
    assert body["run"]["passed"] is True
    version = body["run"]["version"]

    listed = client.get("/registry/packages", params={"client_id": "acme"}).json()
    assert [p["version"] for p in listed] == [version]

    # Promotion is refused until a sign-off exists (CTR-19).
    refused = client.post(f"/registry/packages/acme/ap-invoices/{version}/promote", json={"promoted_by": "joe"})
    assert refused.status_code == 403
    assert refused.json()["detail"]["code"] == "signoff_required"

    assert client.post(
        f"/registry/packages/acme/ap-invoices/{version}/signoff",
        json={"identity": "joe@acme.example"},
    ).status_code == 200
    promoted = client.post(
        f"/registry/packages/acme/ap-invoices/{version}/promote", json={"promoted_by": "joe@acme.example"}
    )
    assert promoted.status_code == 200, promoted.text
    assert promoted.json()["state"] == "promoted"

    active = client.get("/registry/workflows/acme/ap-invoices/active").json()
    assert active["version"] == version

    verified = client.get(f"/registry/packages/acme/ap-invoices/{version}/verify").json()
    assert verified["checksums_ok"] is True
    assert verified["promotion_bytes_match"] is True

    resolved = client.get(
        "/registry/workflows/acme/ap-invoices/resolve", params={"engine_version": "2.1.3"}
    )
    assert resolved.status_code == 200
    incompatible = client.get(
        "/registry/workflows/acme/ap-invoices/resolve", params={"engine_version": "4.0.0"}
    )
    assert incompatible.status_code == 409
    assert incompatible.json()["detail"]["code"] == "engine_incompatible"


def test_authoring_run_refuses_to_generate_without_confirmed_types(client, corpus_dict, corpus_root):
    r = client.post(
        "/runs",
        json={
            "corpus": corpus_dict,
            "corpus_root": str(corpus_root),
            "client_id": "acme",
            "workflow_id": "ap-invoices",
        },
    )
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "confirmation_required"


def test_republishing_the_same_version_is_refused_over_http(client, corpus_dict, corpus_root):
    payload = {
        "corpus": corpus_dict,
        "corpus_root": str(corpus_root),
        "client_id": "acme",
        "workflow_id": "ap-invoices",
        "confirmed_types": ["invoice"],
        "confirmed_critical": {"invoice": ["amount"]},
        "reviewed_by": "joe@acme.example",
        "publish": True,
    }
    assert client.post("/runs", json=payload).status_code == 200
    second = client.post("/runs", json=payload)
    assert second.status_code == 409
    assert second.json()["detail"]["code"] == "version_exists"
