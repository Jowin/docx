"""Config folders as the source of truth: lookups at three levels, releases.json, detection rules,
and multi-type configs."""
from __future__ import annotations

import json
import shutil
from email.message import EmailMessage

from fastapi.testclient import TestClient

from extractor_service.api import create_app
from extractor_service.config_store import ConfigStore
from extractor_service.errors import ConfigError

INVOICE_BODY = ("Please find our invoice below.\n\nInvoice No: INV-77\nInvoice Date: 2026-08-01\n"
                "Supplier: Acme Corp\nTotal Due: $1,250.00\n")
QUOTE_BODY = ("Thanks for your enquiry. Our quotation is below; it is valid for thirty days and is not a "
              "request for payment.\nQuotation reference: Q-501\nEstimate: $900.00\n")
REMIT_BODY = ("Remittance advice: we have paid your invoice.\nPayment Reference: RA-9917\n"
              "Amount Paid: 1250.00\nPayment Date: 2026-09-02\n")

DETECTION = {"schema_version": "1.0", "generated_by": "test", "types": {
    "invoice": {"keywords": [{"term": "invoice", "weight": 0.4}, {"term": "total due", "weight": 0.3}],
                "patterns": [{"name": "inv_no", "regex": r"(?i)\binv-\d+", "weight": 0.2}],
                "entity_weights": {"MONEY": 0.1}, "classification_threshold": 0.6,
                "negative_signals": ["quotation", "not a request for payment", "remittance advice"]},
}}


def _app(settings):
    return create_app(settings, start_workers=False)


def _eml(input_root, name, body, subject="Invoice"):
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, "ap@hooli.example", "ap@buyer.example"
    msg.set_content(body)
    (input_root / name).write_bytes(bytes(msg))
    return name


def _extract(settings, name, **kw):
    return TestClient(_app(settings)).post("/extract", json={"file_location": name, "extended": True, **kw}).json()


def _new_version(config_root, version, client="default", usecase="invoice", base="1.0.0"):
    src = config_root / client / usecase / base
    dst = config_root / client / usecase / version
    shutil.copytree(src, dst)
    m = json.loads((dst / "manifest.json").read_text())
    m["version"] = version
    (dst / "manifest.json").write_text(json.dumps(m))
    return dst


def _releases(config_root, active, client="default", usecase="invoice"):
    (config_root / client / usecase / "releases.json").write_text(json.dumps(
        {"active": active, "history": [{"action": "release", "version": active, "by": "qa@example"}] if active
         else []}))


# ------------------------------------------------------------------ lookups at three levels


def _lookup(config_root, where, data):
    folder = config_root / where / "lookups" if where else config_root / "lookups"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "ingestion.json").write_text(json.dumps(data))


def _with_attachments(input_root, name, *files):
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = "Invoice", "ap@hooli.example", "ap@buyer.example"
    msg.set_content(INVOICE_BODY)
    for fname, data in files:
        msg.add_attachment(data, maintype="application", subtype="octet-stream", filename=fname)
    (input_root / name).write_bytes(bytes(msg))
    return name


ROWS = b"Invoice No,Invoice Date,Supplier,Total Due\nINV-1,2026-08-01,Acme Corp,10.00\n"


def test_client_and_usecase_lookups_apply_only_to_their_scope(settings, config_root, input_root):
    _lookup(config_root, "default", {"ignore_names": ["client-junk"]})
    _lookup(config_root, "default/invoice", {"ignore_names": ["usecase-junk"]})
    _lookup(config_root, "acme", {"ignore_names": ["inv.csv"]})          # another client: not applied
    name = _with_attachments(input_root, "m.eml", ("client-junk.csv", ROWS), ("usecase-junk.csv", ROWS),
                             ("inv.csv", ROWS))
    e = _extract(settings, name)
    rules = {s["item"]: s.get("rule") for s in e["metadata"]["skipped"]}
    assert rules == {"client-junk.csv": "client:name:client-junk", "usecase-junk.csv": "usecase:name:usecase-junk"}
    assert "attachment:inv.csv" in [d["source"] for d in e["metadata"]["documents"]]


def test_a_narrower_level_can_keep_what_a_wider_one_ignores(settings, config_root, input_root):
    _lookup(config_root, "", {"ignore_names": ["docusign"]})
    _lookup(config_root, "default/invoice", {"keep_names": ["docusign invoice"]})
    name = _with_attachments(input_root, "m.eml", ("DocuSign Invoice.csv", ROWS), ("DocuSign Summary.csv", ROWS))
    e = _extract(settings, name)
    assert [s["item"] for s in e["metadata"]["skipped"]] == ["DocuSign Summary.csv"]
    assert "attachment:DocuSign Invoice.csv" in [d["source"] for d in e["metadata"]["documents"]]


def test_lookup_changes_apply_without_a_new_version(settings, config_root, input_root):
    app = TestClient(_app(settings))
    name = _with_attachments(input_root, "m.eml", ("extra.csv", ROWS))
    first = app.post("/extract", json={"file_location": name, "extended": True, "idempotency_key": "a"}).json()
    assert first["metadata"]["skipped"] == []
    _lookup(config_root, "default", {"ignore_names": ["extra"]})
    second = app.post("/extract", json={"file_location": name, "extended": True, "idempotency_key": "b"}).json()
    assert [s["item"] for s in second["metadata"]["skipped"]] == ["extra.csv"]
    assert first["metadata"]["config"]["sha256"] == second["metadata"]["config"]["sha256"]


def test_lookups_endpoint_shows_every_level(settings, config_root):
    _lookup(config_root, "", {"ignore_names": ["docusign"]})
    _lookup(config_root, "default", {"ignore_hashes": ["md5:" + "0" * 32]})
    out = TestClient(_app(settings)).get("/lookups", params={"client": "default", "usecase": "invoice"}).json()
    assert [lvl["level"] for lvl in out["levels"]] == ["usecase", "client", "global"]
    assert [(x["level"], x["ignore_names"], x["ignore_hashes"]) for x in out["layers"]] == \
        [("client", [], ["md5:" + "0" * 32]), ("global", ["docusign"], [])]


def test_lookups_is_not_a_client_or_usecase(config_root):
    store = ConfigStore(config_root)
    _lookup(config_root, "default", {"ignore_names": ["x"]})
    assert "lookups" not in [c["usecase"] for c in store.catalogue()]
    try:
        store.resolve("lookups")
    except ConfigError as exc:
        assert exc.code == "config_invalid_name"
    else:
        raise AssertionError("a client named lookups resolved")


# ------------------------------------------------------------------ releases


def test_releases_json_decides_what_is_served(settings, config_root, input_root):
    _new_version(config_root, "1.0.1")
    _releases(config_root, "1.0.0")
    name = _eml(input_root, "inv.eml", INVOICE_BODY)
    served = _extract(settings, name)["metadata"]["config"]
    assert (served["version"], served["resolved_by"]["version"], served["release_status"]) == \
        ("1.0.0", "release", "active")
    cand = _extract(settings, name, version="1.0.1")["metadata"]["config"]
    assert (cand["version"], cand["release_status"]) == ("1.0.1", "candidate")
    assert _extract(settings, name, version="latest")["metadata"]["config"]["version"] == "1.0.1"


def test_unmanaged_use_case_keeps_the_old_rules(settings, config_root, input_root):
    name = _eml(input_root, "inv.eml", INVOICE_BODY)
    _new_version(config_root, "1.0.1")
    cfg = _extract(settings, name)["metadata"]["config"]
    # defaults.json pins 1.0.0 for the default client and use case; no releases.json
    assert (cfg["version"], cfg["release_status"]) == ("1.0.0", "unmanaged")


def test_a_managed_use_case_with_nothing_released_is_a_flagged_result(settings, config_root, input_root):
    _releases(config_root, None)
    e = _extract(settings, _eml(input_root, "inv.eml", INVOICE_BODY))
    assert e["flagged"] is True and e["flags"] == ["error:config_not_released"]


def test_catalogue_and_release_endpoint(settings, config_root):
    _new_version(config_root, "1.0.1")
    _new_version(config_root, "0.9.0")
    _releases(config_root, "1.0.0")
    app = TestClient(_app(settings))
    entry = next(c for c in app.get("/configs").json()["configs"] if c["client"] == "default")
    assert entry["active"] == "1.0.0" and entry["managed"] is True
    assert entry["status"] == {"0.9.0": "retired", "1.0.0": "active", "1.0.1": "candidate"}
    rel = app.get("/configs/default/invoice/releases").json()
    assert rel["active"] == "1.0.0" and rel["history"][0]["by"] == "qa@example"


# ------------------------------------------------------------------ detection rules


def _with_detection(config_root, version="1.0.0", detection=DETECTION, manifest_extra=None):
    folder = config_root / "default" / "invoice" / version
    (folder / "rules").mkdir(exist_ok=True)
    (folder / "rules" / "detection.json").write_text(json.dumps(detection))
    if manifest_extra:
        m = json.loads((folder / "manifest.json").read_text())
        m.update(manifest_extra)
        (folder / "manifest.json").write_text(json.dumps(m))
    return folder


def test_detection_rules_classify_before_extraction(settings, config_root, input_root):
    _with_detection(config_root)
    e = _extract(settings, _eml(input_root, "inv.eml", INVOICE_BODY))
    c = e["metadata"]["classification"]
    assert c["status"] == "matched" and c["type"] == "invoice" and c["score"] >= 0.6
    assert "out_of_scope" not in e["flags"]
    assert e["data"][0]["invoice_number"] == "INV-77"


def test_out_of_scope_email_is_not_extracted(settings, config_root, input_root):
    _with_detection(config_root)
    e = _extract(settings, _eml(input_root, "q.eml", QUOTE_BODY, subject="Quotation Q-501"))
    c = e["metadata"]["classification"]
    assert c["status"] == "out_of_scope" and c["scores"]["invoice"]["negative_signals"]
    assert e["flagged"] is True and "out_of_scope" in e["flags"]
    assert e["data"] == [] and e["records"] == []
    assert "extract" not in e["metadata"]["graph"]["path"]


def test_out_of_scope_can_still_extract_when_the_manifest_says_so(settings, config_root, input_root):
    _with_detection(config_root, manifest_extra={"classification": {"out_of_scope": "extract"}})
    e = _extract(settings, _eml(input_root, "q.eml", QUOTE_BODY, subject="Quotation Q-501"))
    assert "out_of_scope" in e["flags"] and "extract" in e["metadata"]["graph"]["path"]


def test_bad_detection_rules_are_a_config_problem(settings, config_root):
    _with_detection(config_root, detection={"types": {"invoice": {"patterns": [{"regex": "(", "weight": 1}]}}})
    health = TestClient(_app(settings)).get("/health").json()
    assert health["status"] == "degraded"
    assert any("detection.json" in p["message"] for p in health["config_problems"])


# ------------------------------------------------------------------ several email types


REMIT_SCHEMA = {"name": "remittance", "version": "1.0.0", "record_key": "payment_reference", "fields": [
    {"name": "payment_reference", "type": "string", "required": True, "aliases": ["payment reference"]},
    {"name": "amount_paid", "type": "decimal", "required": True, "critical": True, "aliases": ["amount paid"]},
    {"name": "payment_date", "type": "date", "aliases": ["payment date"]},
]}


def _two_types(config_root):
    folder = _new_version(config_root, "2.0.0")
    (folder / "schemas").mkdir()
    (folder / "schemas" / "remittance.json").write_text(json.dumps(REMIT_SCHEMA))
    detection = json.loads(json.dumps(DETECTION))
    detection["types"]["invoice"]["negative_signals"] = ["quotation", "remittance advice"]
    detection["types"]["remittance"] = {
        "keywords": [{"term": "remittance", "weight": 0.5}, {"term": "amount paid", "weight": 0.3}],
        "patterns": [], "entity_weights": {}, "classification_threshold": 0.6, "negative_signals": []}
    m = json.loads((folder / "manifest.json").read_text())
    m["skills"] = []
    m["types"] = {"invoice": {"schema": "schema.json", "skills": ["field-extraction", "table-extraction"]},
                  "remittance": {"schema": "schemas/remittance.json", "thresholds": {"accept_at": 0.5}}}
    (folder / "manifest.json").write_text(json.dumps(m))
    _with_detection(config_root, "2.0.0", detection)
    return folder


def test_each_email_type_uses_its_own_schema(settings, config_root, input_root):
    _two_types(config_root)
    inv = _extract(settings, _eml(input_root, "inv.eml", INVOICE_BODY), version="2.0.0")
    rem = _extract(settings, _eml(input_root, "rem.eml", REMIT_BODY, subject="Payment advice"), version="2.0.0")
    assert inv["metadata"]["classification"]["type"] == "invoice"
    assert inv["metadata"]["config"]["email_type"] == "invoice"
    assert "invoice_number" in inv["data"][0]
    assert rem["metadata"]["classification"]["type"] == "remittance"
    assert rem["metadata"]["config"]["email_type"] == "remittance"
    assert rem["data"][0]["payment_reference"] == "RA-9917" and rem["data"][0]["amount_paid"] == 1250.0
    assert rem["metadata"]["skills_applied"] == []


def test_config_endpoint_describes_types_and_detection(settings, config_root):
    _two_types(config_root)
    d = TestClient(_app(settings)).get("/configs/default/invoice/2.0.0").json()
    assert sorted(d["types"]) == ["invoice", "remittance"] and d["default_type"] == "invoice"
    assert d["types"]["remittance"]["accept_at"] == 0.5
    assert d["detection"]["remittance"]["keywords"] == 2


def test_a_type_without_its_schema_file_is_incomplete(config_root):
    folder = _two_types(config_root)
    (folder / "schemas" / "remittance.json").unlink()
    try:
        ConfigStore(config_root).resolve("default", "invoice", "2.0.0")
    except ConfigError as exc:
        assert exc.code == "config_incomplete"
    else:
        raise AssertionError("loaded without the remittance schema")


def test_two_close_matches_are_ambiguous(settings, config_root, input_root):
    folder = _two_types(config_root)
    det = json.loads((folder / "rules" / "detection.json").read_text())
    det["types"]["invoice"]["negative_signals"] = []
    (folder / "rules" / "detection.json").write_text(json.dumps(det))
    m = json.loads((folder / "manifest.json").read_text())
    m["classification"] = {"ambiguity_margin": 0.3}          # invoice scores 1.0, remittance 0.8
    (folder / "manifest.json").write_text(json.dumps(m))
    body = "Remittance advice for invoice INV-77. Amount paid: 1250.00. Total due: $0.00\n"
    e = _extract(settings, _eml(input_root, "both.eml", body, subject="Remittance"), version="2.0.0")
    c = e["metadata"]["classification"]
    assert c["status"] == "ambiguous" and {c["type"], c["runner_up"]} == {"invoice", "remittance"}
    assert any(f.startswith("classification_ambiguous:") for f in e["flags"]) and e["flagged"] is True


def test_inputs_and_forced_extended_view(settings, input_root):
    app = TestClient(_app(settings))
    names = [f["path"] for f in app.get("/inputs").json()["files"]]
    assert "invoice.pdf" in names
    job = app.post("/extract", json={"file_location": "statement.csv", "async": True}).json()["job_id"]
    app.app.state.runner.drain()
    plain = app.get(f"/extractions/{job}").json()["result"]
    full = app.get(f"/extractions/{job}", params={"extended": True}).json()["result"]
    assert isinstance(full, dict) and "metadata" in full
    assert plain == full or plain == full["data"]
