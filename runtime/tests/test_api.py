import json

import pytest

from tests.conftest import data_of

FIELDS = ["invoice_number", "invoice_date", "due_date", "vendor", "currency",
          "total_amount", "tax_amount", "po_number", "line_items"]


def test_plain_output_is_an_array_of_records(client):
    r = client.post("/extract", json={"file_location": "invoice-email.eml"})
    assert r.status_code == 200
    records = r.json()
    assert isinstance(records, list) and len(records) == 1
    body = records[0]
    assert list(body) == FIELDS
    assert body["invoice_number"] == "INV-20194" and body["total_amount"] == 12400
    assert body["line_items"][0] == {"description": "Consulting", "quantity": 10,
                                     "unit_price": 1000, "amount": 10000}
    assert r.headers["x-extraction-status"] == "extracted" and r.headers["x-extraction-flagged"] == "false"
    assert r.headers["x-audit-id"].startswith("run_") and r.headers["x-job-id"].startswith("job_")


def test_extended_output_has_confidence_sources_and_metadata(client):
    e = client.post("/extract", json={"file_location": "invoice-email.eml", "extended": True}).json()
    assert e["status"] == "extracted" and e["flagged"] is False and e["flags"] == []
    assert len(e["data"]) == 1 and list(e["data"][0]) == FIELDS
    rec = e["records"][0]
    assert rec["flagged"] is False and rec["data"] == e["data"][0]
    total = rec["fields"]["total_amount"]
    assert total["source"] == "attachment:INV-20194.xlsx#Invoice!D13"
    assert total["grounding"] == "verified" and 0 < total["confidence"] <= 1
    assert rec["fields"]["currency"]["grounding"] == "inferred"
    meta = e["metadata"]
    assert meta["record_count"] == 1 and meta["record_key"] == "invoice_number"
    assert meta["config"]["resolved_by"]["client"] == "default"
    assert meta["model"]["provider"] == "stub"
    assert meta["input"]["kind"] == "email" and meta["input"]["subject"].startswith("Invoice INV-20194")
    assert meta["skipped"] == [{"item": "logo.png", "reason": "image_not_supported"}]
    assert [d["source"] for d in meta["documents"]] == ["body", "attachment:INV-20194.xlsx"]


@pytest.mark.parametrize("name,expect", [
    ("invoice.xlsx", {"invoice_number": "INV-20194", "due_date": "2026-09-14", "total_amount": 12400}),
    ("statement.csv", {"invoice_number": "INV-30001", "invoice_date": "2026-08-15",
                       "total_amount": 4250.75, "po_number": "PO-7700"}),
    ("invoice.pdf", {"invoice_number": "INI-0042", "vendor": "Initech LLC", "total_amount": 1500}),
    ("bundle.zip", {"invoice_number": "INV-30001", "vendor": "Globex Ltd"}),
])
def test_each_input_kind(client, name, expect):
    [body] = data_of(client.post("/extract", json={"file_location": name}))
    for k, v in expect.items():
        assert body[k] == v, k


def test_zip_member_sources_carry_the_zip_path(client):
    e = client.post("/extract", json={"file_location": "bundle.zip", "extended": True}).json()
    assert e["records"][0]["fields"]["vendor"]["source"] == "file:bundle.zip/statement.csv#D2"
    assert {"item": "bundle.zip/scans/photo.png", "reason": "image_not_supported"} in e["metadata"]["skipped"]


def test_pdf_line_items_from_its_table(client):
    [body] = data_of(client.post("/extract", json={"file_location": "invoice.pdf"}))
    assert [i["description"] for i in body["line_items"]] == ["Hosting", "Support"]


def test_versioned_config_changes_output(stub_client):
    r = stub_client.post("/extract", json={"file_location": "invoice.xlsx", "client": "acme",
                                           "usecase": "ap-invoices", "version": "1.1.0"})
    [body] = data_of(r)
    assert "payment_reference" in body
    assert body["total_amount"] == "12400"            # acme configs output decimals as text
    old = stub_client.post("/extract", json={"file_location": "invoice.xlsx", "client": "acme",
                                             "version": "1.0.0"}).json()
    assert "payment_reference" not in data_of(old)[0]


def test_gateway_config_without_gateway_settings_is_a_flagged_result(client):
    r = client.post("/extract", json={"file_location": "invoice.xlsx", "client": "acme"})
    e = r.json()
    assert r.status_code == 200 and r.headers["x-extraction-flagged"] == "true"
    assert e["flags"] == ["error:model_unavailable"] and e["data"] == []
    assert e["metadata"]["model_error"]["flag"] == "error:model_unavailable"


@pytest.mark.parametrize("body,code", [
    ({"file_location": "../../etc/passwd"}, "location_outside_input_root"),
    ({"file_location": "/etc/passwd"}, "location_outside_input_root"),
    ({"file_location": "missing.csv"}, "file_not_found"),
    ({"file_location": "logo.png"}, "unsupported_input:image"),
    ({"file_location": "invoice.xlsx", "client": "nobody"}, "config_not_found"),
])
def test_a_failed_run_is_still_a_result_flagged_with_its_error(client, body, code):
    r = client.post("/extract", json=body)
    e = r.json()
    assert r.status_code == 200 and r.headers["x-extraction-status"] == "extracted"
    assert e["flagged"] is True and e["data"] == [] and e["flags"] == [f"error:{code}"]
    assert e["error"]["code"] == code


@pytest.mark.parametrize("body,loc,msg", [
    ({"file_location": "invoice.xlsx", "extnded": True}, "extnded", "unknown field"),
    ({"client": "acme"}, "file_location", "required"),
    ({"file_location": "invoice.xlsx", "extended": "yes"}, "extended", "must be a boolean"),
    ({"file_location": 7}, "file_location", "must be a non-empty string"),
])
def test_request_is_validated(client, body, loc, msg):
    r = client.post("/extract", json=body)
    assert r.status_code == 422 and r.json()["error"] == "invalid_request"
    assert {"loc": loc, "msg": msg} in r.json()["detail"]["errors"]


def test_body_that_is_not_json(client):
    r = client.post("/extract", content=b"{nope", headers={"content-type": "application/json"})
    assert r.status_code == 422 and r.json()["error"] == "invalid_json"


def test_request_schema_is_published(client):
    body = client.get("/openapi.json").json()["paths"]["/extract"]["post"]["requestBody"]
    schema = body["content"]["application/json"]["schema"]
    assert schema["required"] == ["file_location"] and schema["additionalProperties"] is False


def test_no_pydantic_in_service_code():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    hits = [str(p.relative_to(root)) for d in ("extractor_service", "extractor_tools")
            for p in (root / d).rglob("*.py") if "pydantic" in p.read_text(encoding="utf-8")]
    assert hits == []


def test_same_input_same_output_and_audit_id(client):
    a = client.post("/extract", json={"file_location": "invoice-email.eml"})
    b = client.post("/extract", json={"file_location": "invoice-email.eml"})
    assert a.content == b.content and a.headers["x-audit-id"] == b.headers["x-audit-id"]


def test_audit_record_written(client, settings):
    r = client.post("/extract", json={"file_location": "invoice.pdf"})
    audit = json.loads((settings.audit_dir / f"{r.headers['x-audit-id']}.json").read_text())
    assert audit["data"] == data_of(r)


def test_missing_required_field_is_flagged_and_returned_extended(client, input_root):
    (input_root / "partial.csv").write_text("Supplier,Total Due\nGlobex Ltd,\"$10.00\"\n")
    r = client.post("/extract", json={"file_location": "partial.csv"})     # not asked for extended
    e = r.json()
    assert r.headers["x-extraction-status"] == "extracted" and r.headers["x-extraction-flagged"] == "true"
    assert e["status"] == "extracted" and e["flagged"] is True
    assert "missing_field:invoice_number" in e["flags"]
    assert e["records"][0]["flagged"] is True and e["records"][0]["fields"]["vendor"]["source"]
    assert e["data"][0]["vendor"] == "Globex Ltd"        # partial data still returned


def test_configs_and_health(client):
    c = client.get("/configs").json()
    assert c["defaults"]["client"] == "default"
    acme = next(x for x in c["configs"] if x["client"] == "acme")
    assert (acme["usecase"], acme["versions"], acme["latest"], acme["managed"]) == \
        ("ap-invoices", ["1.0.0", "1.1.0"], "1.1.0", False)
    one = client.get("/configs/acme/ap-invoices/1.1.0").json()
    assert one["manifest"]["model"]["provider"] == "gateway"
    assert client.get("/health").json()["status"] == "ok"
