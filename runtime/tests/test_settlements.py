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


# ------------------------------------------------------------------ whole-file blotters (expand)

BLOTTER_HEADER = "Trade Date,Settlement Date,Portfolio,Transaction Type,Security ID,CCY,Net Amount,Purpose Code,Comments\n"


def _blotter(path, n, *, total=True):
    with open(path, "w") as f:
        f.write(BLOTTER_HEADER)
        for i in range(n):
            f.write(f"2026-10-01,2026-10-03,PF-{i % 3},BUY,US{i:09d}0,USD,\"{i:,}.25\",SECU,row {i}\n")
        if total:
            f.write("Total,,,,,,,,\n")


def test_every_row_of_a_large_blotter_is_extracted(app, input_root):
    _blotter(input_root / "big.csv", 1200)
    e = _run(app, "big.csv")
    assert len(e["data"]) == 1200 and e["flags"] == []
    assert [r["comments"] for r in e["data"][:3]] == ["row 0", "row 1", "row 2"]
    assert e["data"][700] == {"settlement_date": "2026-10-03", "currency": "USD", "amount": 700.25,
                              "trade_date": "2026-10-01", "comments": "row 700", "portfolio": "PF-1",
                              "cash_purpose_code": "SECU", "transaction_type": "BUY", "security_id": "US0000007000"}
    assert e["data"][-1]["comments"] == "row 1199"                     # the total row is not a record
    [rep] = e["metadata"]["expanded"]
    assert rep["complete"] is True and rep["records"] == 1200 - 209        # 200 head + 9 tail rows answered
    assert rep["mapping"]["amount"] == "Net Amount"
    assert e["records"][700]["fields"]["amount"]["source"] == "file:big.csv#G702"


def test_a_large_sheet_with_a_title_block(app, input_root):
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.title = "Blotter"
    ws.append(["Daily settlements"])
    ws.append([])
    ws.append(BLOTTER_HEADER.strip().split(","))
    for i in range(600):
        ws.append(["2026-10-01", "2026-10-03", "PF-9", "SELL", f"GB{i:09d}0", "GBP", i + 0.5, "SECU", f"line {i}"])
    wb.save(input_root / "big.xlsx")
    e = _run(app, "big.xlsx")
    assert len(e["data"]) == 600 and e["data"][599]["comments"] == "line 599"
    assert e["data"][300]["amount"] == 300.5 and e["data"][300]["currency"] == "GBP"


def test_a_value_labelled_once_fills_every_streamed_row(app, input_root):
    from email.message import EmailMessage
    rows = "Settlement Date,CCY,Net Amount,Transaction Type\n" + "".join(
        f"2026-10-03,USD,{i}.00,BUY\n" for i in range(1, 501))
    m = EmailMessage()
    m["Subject"], m["From"], m["To"] = "Settlements", "ops@custodian.example", "x@y.example"
    m.set_content("Settlement blotter attached.\nPortfolio: GLB-EQ-01\n")
    m.add_attachment(rows.encode(), maintype="text", subtype="csv", filename="b.csv")
    (input_root / "big.eml").write_bytes(bytes(m))
    e = _run(app, "big.eml")
    assert len(e["data"]) == 500 and {r["portfolio"] for r in e["data"]} == {"GLB-EQ-01"}
    assert e["metadata"]["expanded"][0]["constants"] == {"portfolio": "GLB-EQ-01"}


def test_the_record_cap_flags_what_it_leaves_out(app, settings, config_root, input_root):
    m = config_root / "default/settlements/1.0.0/manifest.json"
    manifest = json.loads(m.read_text())
    manifest["limits"] = {"max_records": 500}
    m.write_text(json.dumps(manifest))
    _blotter(input_root / "big.csv", 1000)
    e = _run(app, "big.csv")
    assert len(e["data"]) == 500
    assert "records_truncated:500" in e["flags"] and "content_truncated:big.csv" in e["flags"]


def test_the_model_maps_the_columns_and_code_reads_the_rows(settings, config_root, input_root):
    """Headers the dictionary has no label for: the model's citations give the mapping."""
    from tests.test_llm_path import FakeGateway
    (config_root / "defaults.json").write_text((ROOT / "configs" / "defaults.json").read_text())
    with open(input_root / "odd.csv", "w") as f:
        f.write("TD,Val Dt,Book,Side,Instr,Cur,Consideration,Why,Free Text\n")
        for i in range(800):
            f.write(f"2026-10-01,2026-10-03,BK{i % 2},BUY,US{i:09d}0,USD,{i}.75,SECU,n{i}\n")

    def rec(r):
        cols = dict(zip(FIELDS, ["B", "F", "G", "A", "I", "C", "H", "D", "E"]))
        vals = {"settlement_date": "2026-10-03", "currency": "USD", "amount": f"{r - 2}.75",
                "trade_date": "2026-10-01", "comments": f"n{r - 2}", "portfolio": f"BK{(r - 2) % 2}",
                "cash_purpose_code": "SECU", "transaction_type": "BUY", "security_id": f"US{r - 2:09d}0"}
        return {k: {"value": vals[k], "source": f"d1#{cols[k]}{r}", "confidence": 0.95} for k in FIELDS}

    gw = FakeGateway({"records": [rec(r) for r in range(2, 202)]})
    s = replace(settings, model_provider="gateway")
    e = TestClient(create_app(s, model_gateway=gw, start_workers=False)).post(
        "/extract", json={"file_location": "odd.csv", "extended": True}).json()
    assert len(gw.requests) == 1 and len(e["data"]) == 800
    assert e["data"][650]["amount"] == 650.75 and e["data"][650]["portfolio"] == "BK0"
    assert e["metadata"]["expanded"][0]["mapping"]["amount"] == "Consideration"


def test_other_use_cases_flag_content_they_did_not_read(settings, input_root):
    from tests.test_large_files import _big_csv
    _big_csv(input_root / "inv.csv", 0.05)
    e = TestClient(create_app(settings, start_workers=False)).post(
        "/extract", json={"file_location": "inv.csv", "extended": True}).json()
    assert "content_truncated:inv.csv" in e["flags"] and e["flagged"] is True
