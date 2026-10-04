"""The shipped default: settlement instructions, one record per instruction."""
from __future__ import annotations

import csv
import io
import json
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from extractor_service.api import create_app
from tests.conftest import ROOT

FIELDS = ["settlement_date", "currency", "amount", "trade_date", "comments", "portfolio", "cash_purpose_code",
          "transaction_type", "security_id"]


@pytest.fixture
def app(settings, config_root):
    (config_root / "defaults.json").write_text((ROOT / "configs" / "defaults.json").read_text())
    return TestClient(create_app(settings, start_workers=False))


def _run(app, name, **kw):
    return app.post("/extract", json={"file_location": name, "extended": True, **kw}).json()


def test_settlements_is_the_default(app):
    d = app.get("/configs").json()["defaults"]
    assert (d["client"], d["usecase"], d["version"]) == ("default", "settlements", "1.0.0")
    e = _run(app, "settlement-blotter.csv")
    cfg = e["metadata"]["config"]
    assert (cfg["usecase"], cfg["resolved_by"]["usecase"]) == ("settlements", "default")
    assert list(e["data"][0]) == FIELDS


def test_a_blotter_is_one_record_per_row(app):
    e = _run(app, "settlement-blotter.csv")
    assert e["flags"] == [] and e["metadata"]["record_count"] == 3 and e["metadata"]["record_key"] == "@row"
    assert e["data"][0] == {"settlement_date": "2026-10-03", "currency": "USD", "amount": -1250000,
                            "trade_date": "2026-10-01", "comments": "Apple purchase", "portfolio": "GLB-EQ-01",
                            "cash_purpose_code": "SECU", "transaction_type": "BUY", "security_id": "US0378331005"}
    third = e["data"][2]
    assert (third["amount"], third["currency"], third["security_id"], third["transaction_type"]) == \
        (-12500, "EUR", None, "CASH OUT")


def test_a_single_instruction_in_an_email_body(app):
    e = _run(app, "settlement-instruction.eml")
    [rec] = e["data"]
    assert rec == {"settlement_date": "2026-10-06", "currency": "USD", "amount": 2000000,
                   "trade_date": "2026-10-01", "comments": "Partial delivery accepted", "portfolio": "GLB-FI-02",
                   "cash_purpose_code": "SECU", "transaction_type": "DVP", "security_id": "XS1234567890"}
    assert e["metadata"]["classification"]["status"] == "matched"


def test_a_value_labelled_once_fills_rows_without_one(app, config_root, input_root):
    (input_root / "nofolio.csv").write_text(
        "Settlement Date,CCY,Net Amount,Transaction Type\n2026-10-03,USD,100.00,BUY\n2026-10-03,USD,200.00,SELL\n")
    from email.message import EmailMessage
    m = EmailMessage()
    m["Subject"], m["From"], m["To"] = "Settlements", "ops@custodian.example", "x@y.example"
    m.set_content("Portfolio: GLB-EQ-01\n")
    m.add_attachment((input_root / "nofolio.csv").read_bytes(), maintype="text", subtype="csv", filename="b.csv")
    (input_root / "nofolio.eml").write_bytes(bytes(m))
    e = _run(app, "nofolio.eml")
    assert [r["portfolio"] for r in e["data"]] == ["GLB-EQ-01", "GLB-EQ-01"]
    assert "unplaced_content" not in e["flags"]


def test_the_default_result_file_is_a_settlement_csv(app, settings, config_root, tmp_path):
    s = replace(settings, output_root=tmp_path / "out")
    (config_root / "defaults.json").write_text((ROOT / "configs" / "defaults.json").read_text())
    e = TestClient(create_app(s, start_workers=False)).post(
        "/extract", json={"file_location": "settlement-blotter.eml", "extended": True}).json()
    [out] = e["metadata"]["outputs"]
    rows = list(csv.reader(io.StringIO((tmp_path / "out" / out["path"]).read_text(encoding="utf-8-sig"))))
    assert rows[0][:10] == ["_record", *FIELDS] and len(rows) == 4
    assert out["path"].startswith("default/settlements/")


def test_the_manifest_and_schema_ship_together():
    folder = ROOT / "configs" / "default" / "settlements" / "1.0.0"
    schema = json.loads((folder / "schema.json").read_text())
    assert [f["name"] for f in schema["fields"]] == FIELDS
    assert {f["name"] for f in schema["fields"] if f.get("required")} == \
        {"settlement_date", "currency", "amount", "portfolio"}
