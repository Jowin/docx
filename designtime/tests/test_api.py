"""Every component over HTTP, one endpoint at a time, then the whole run."""

from __future__ import annotations

import json
import sys

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
    "extraction-judge",
    "pattern-skill-writer",
}


from pathlib import Path

RUNTIME_DIR = Path(__file__).resolve().parents[2] / "runtime"


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


def test_authoring_run_publishes_a_candidate_that_a_person_releases(client, corpus_dict, corpus_root,
                                                                   runtime_configs):
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
            "requested_by": "ana@acme.example",
        },
    )
    assert run.status_code == 200, run.text
    body = run.json()
    assert body["published"] is True and body["passed"] is True, body["eval_report"]["gate_results"]
    assert (body["base_version"], body["version"]) == ("1.1.0", "1.2.0")
    assert body["eval_report"]["engine"] == "runtime"
    assert body["eval_report"]["scope"]["turned_away"] == body["eval_report"]["scope"]["out_of_scope_samples"]
    assert {"manifest.json", "schema.json", "rules/detection.json", "prompts/system.md",
            "provenance.json"} <= set(body["files"])
    assert body["manifest"]["types"]["invoice"]["schema"] == "schema.json"

    # the folder is the truth, and nothing went live
    root = runtime_configs
    releases = json.loads((root / "acme/ap-invoices/releases.json").read_text())
    assert releases["active"] == "1.1.0"
    usecase = client.get("/configs/acme/ap-invoices").json()
    assert {v["version"]: v["status"] for v in usecase["versions"]} == \
        {"1.0.0": "retired", "1.1.0": "active", "1.2.0": "candidate"}
    new = next(v for v in usecase["versions"] if v["version"] == "1.2.0")
    assert new["origin"] == "authoring" and new["record"]["gates_passed"] is True

    # the run is logged with the learning runs
    runs = client.get("/learning/runs", params={"kind": "authoring"}).json()
    assert [(r["outcome"], r["result_version"]) for r in runs] == [("published", "1.2.0")]

    # the runtime itself loads it, with its types and detection rules
    sys.path.insert(0, str(RUNTIME_DIR))
    from extractor_service.config_store import ConfigStore
    cfg = ConfigStore(root).resolve("acme", "ap-invoices", "1.2.0")
    assert list(cfg.types) == ["invoice"] and cfg.detection and cfg.release_status == "candidate"
    assert ConfigStore(root).resolve("acme", "ap-invoices").version == "1.1.0"

    # release: needs a sign-off, and not by the person who produced it (CTR-19)
    url = "/configs/acme/ap-invoices/1.2.0"
    refused = client.post(f"{url}/release", json={"by": "joe@acme.example"})
    assert refused.status_code == 403 and refused.json()["detail"]["code"] == "signoff_required"
    own = client.post(f"{url}/signoff", json={"identity": "ana@acme.example"})
    assert own.status_code == 403 and own.json()["detail"]["code"] == "self_signoff"
    assert client.post(f"{url}/signoff", json={"identity": "joe@acme.example"}).status_code == 201
    released = client.post(f"{url}/release", json={"by": "joe@acme.example", "note": "UAT ok"})
    assert released.status_code == 200, released.text
    assert released.json()["active"] == "1.2.0" and released.json()["previous"] == "1.1.0"
    assert ConfigStore(root).resolve("acme", "ap-invoices").version == "1.2.0"

    diff = client.get(f"{url}/diff", params={"against": "1.1.0"}).text
    assert "+++ 1.2.0/rules/detection.json" in diff

    # rollback goes to the version released before
    back = client.post("/configs/acme/ap-invoices/rollback", json={"by": "joe@acme.example", "note": "incident"})
    assert back.status_code == 200 and back.json()["active"] == "1.1.0"
    log = client.get("/configs/acme/ap-invoices").json()["release_log"]
    assert [(e["action"], e["version"]) for e in log] == [("rollback", "1.1.0"), ("release", "1.2.0")]

    # a folder edited after publication cannot be released again
    (root / "acme/ap-invoices/1.2.0/prompts/system.md").write_text("changed")
    assert client.get(f"{url}/verify").json()["intact"] is False
    tampered = client.post(f"{url}/release", json={"by": "joe@acme.example"})
    assert tampered.status_code == 422 and tampered.json()["detail"]["code"] == "config_tampered"

    # reject: never "latest" again
    assert client.post(f"{url}/reject", json={"by": "joe@acme.example"}).status_code == 200
    assert ConfigStore(root).resolve("acme", "ap-invoices", "latest").version == "1.1.0"


def test_a_failed_gate_needs_an_explicit_override_to_release(client, corpus_dict, corpus_root):
    payload = {"corpus": corpus_dict, "corpus_root": str(corpus_root), "client_id": "acme",
               "workflow_id": "ap-invoices", "confirmed_types": ["invoice"], "requested_by": "ana@acme.example",
               "confirmed_critical": {"invoice": ["amount"]}}
    body = client.post("/runs", json=payload).json()
    version = body["version"]
    from dataextractor_designtime.registry.db import get_sessionmaker
    from dataextractor_designtime.registry.models import ConfigVersionRow
    with get_sessionmaker(client.app.state.database_url)() as s:     # pretend a gate failed
        row = s.query(ConfigVersionRow).filter_by(version=version).one()
        row.gates_passed = False
        s.commit()
    url = f"/configs/acme/ap-invoices/{version}"
    client.post(f"{url}/signoff", json={"identity": "joe@acme.example"})
    refused = client.post(f"{url}/release", json={"by": "joe@acme.example"})
    assert refused.status_code == 403 and refused.json()["detail"]["code"] == "gate_failed"
    no_note = client.post(f"{url}/release", json={"by": "joe@acme.example", "accept_gate_failure": True})
    assert no_note.status_code == 403
    ok = client.post(f"{url}/release", json={"by": "joe@acme.example", "accept_gate_failure": True,
                                             "note": "attribution gate is known-noisy on this corpus"})
    assert ok.status_code == 200
    log = client.get("/configs/acme/ap-invoices").json()["release_log"]
    assert log[0]["gate_override"] is True


def test_lookups_are_edited_per_level(client, runtime_configs):
    r = client.put("/lookups", json={"ignore_names": ["docusign"], "ignore_hashes": ["md5:" + "a" * 32]})
    assert r.status_code == 200 and r.json()["level"] == "global"
    client.put("/lookups", json={"client": "acme", "keep_names": ["docusign invoice"]})
    client.put("/lookups", json={"client": "acme", "usecase": "ap-invoices", "ignore_kinds": ["image"]})
    view = client.get("/lookups", params={"client": "acme", "usecase": "ap-invoices"}).json()
    assert [(lv["level"], bool(lv["ingestion"])) for lv in view["levels"]] == \
        [("usecase", True), ("client", True), ("global", True)]
    assert json.loads((runtime_configs / "acme/lookups/ingestion.json").read_text()) == \
        {"keep_names": ["docusign invoice"]}
    bad = client.put("/lookups", json={"ignore_names": ["re:("]})
    assert bad.status_code == 422 and bad.json()["detail"]["code"] == "invalid_regex"
    assert client.put("/lookups", json={"usecase": "x"}).status_code == 400


def test_graphs_are_published_as_mermaid(client):
    assert "build_config" in client.get("/graphs/authoring").text
    assert "test_candidate" in client.get("/graphs/learning").text


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
