"""Output is always an array of records shaped by the data dictionary."""
import io
from email.message import EmailMessage

from fastapi.testclient import TestClient

from extractor_service.api import create_app
from extractor_service.config_store import ConfigStore
from extractor_service.errors import ConfigError
from extractor_service.schema import load_dictionary
from tests.test_llm_path import FakeGateway, _record

import pytest


def _pdf(number: str, total: str) -> bytes:
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import Paragraph, SimpleDocTemplate
    buf, st = io.BytesIO(), getSampleStyleSheet()
    SimpleDocTemplate(buf, invariant=1).build([
        Paragraph(f"Invoice No: {number}", st["Normal"]), Paragraph("Invoice Date: 2026-09-10", st["Normal"]),
        Paragraph("Supplier: Initech LLC", st["Normal"]), Paragraph(f"Total Due: ${total}", st["Normal"])])
    return buf.getvalue()


def test_one_record_per_row_when_a_table_has_the_key_column(client):
    r = client.post("/extract", json={"file_location": "invoices.csv", "extended": True})
    e = r.json()
    assert [d["invoice_number"] for d in e["data"]] == ["INV-1001", "INV-1002", "INV-1003"]
    assert [d["total_amount"] for d in e["data"]] == [1250, 980.4, 3100]
    assert [rec["fields"]["vendor"]["source"] for rec in e["records"]] == [
        "file:invoices.csv#D2", "file:invoices.csv#D3", "file:invoices.csv#D4"]
    assert all(rec["status"] == "extracted" for rec in e["records"])
    assert e["metadata"]["record_count"] == 3 and r.headers["x-extraction-status"] == "extracted"


def test_rows_sharing_a_key_become_one_record_with_line_items(client, input_root):
    (input_root / "lines.csv").write_text(
        "Invoice No,Invoice Date,Supplier,Total Due,Description,Qty,Amount\n"
        "INV-7,2026-09-01,Acme Corp,1400,Consulting,10,1000\n"
        "INV-7,2026-09-01,Acme Corp,1400,Licence,1,400\n"
        "INV-8,2026-09-02,Acme Corp,200,Support,2,200\n")
    data = client.post("/extract", json={"file_location": "lines.csv"}).json()
    assert [d["invoice_number"] for d in data] == ["INV-7", "INV-8"]
    assert [i["description"] for i in data[0]["line_items"]] == ["Consulting", "Licence"]
    assert data[1]["line_items"] == [{"description": "Support", "quantity": 2, "unit_price": None, "amount": 200}]


def test_labels_outside_the_table_fill_every_record(client, input_root):
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Statement"
    ws["A1"] = "Supplier: Globex Ltd"
    ws.append([])
    ws.append(["Invoice No", "Invoice Date", "Total Due"])
    ws.append(["G-100", "2026-07-01", 50.0])
    ws.append(["G-101", "2026-07-15", 75.25])
    buf = io.BytesIO()
    wb.save(buf)
    (input_root / "statement.xlsx").write_bytes(buf.getvalue())
    e = client.post("/extract", json={"file_location": "statement.xlsx", "extended": True}).json()
    assert [(d["invoice_number"], d["vendor"]) for d in e["data"]] == [("G-100", "Globex Ltd"), ("G-101", "Globex Ltd")]
    assert e["records"][1]["fields"]["vendor"]["source"] == "file:statement.xlsx#Statement!A1"


def test_each_attached_invoice_is_its_own_record(client, input_root):
    m = EmailMessage()
    m["From"] = "ap@initech.example"
    m["Subject"] = "Two invoices"
    m.set_content("Please find two invoices attached.")
    for no, total in (("INI-1", "10.00"), ("INI-2", "20.00")):
        m.add_attachment(_pdf(no, total), maintype="application", subtype="pdf", filename=f"{no}.pdf")
    (input_root / "two.eml").write_bytes(bytes(m))
    e = client.post("/extract", json={"file_location": "two.eml", "extended": True}).json()
    assert [(d["invoice_number"], d["total_amount"]) for d in e["data"]] == [("INI-1", 10), ("INI-2", 20)]
    assert e["records"][1]["fields"]["total_amount"]["source"] == "attachment:INI-2.pdf#p1:L4"


def test_body_and_attachment_about_the_same_invoice_merge(client):
    data = client.post("/extract", json={"file_location": "invoice-email.eml"}).json()
    assert len(data) == 1 and data[0]["invoice_number"] == "INV-20194"


def test_content_that_fits_no_record_is_flagged(client, input_root):
    m = EmailMessage()
    m["Subject"] = "Two invoices"
    m.set_content("Total due: $30.00 for both.")
    for no, total in (("INI-1", "10.00"), ("INI-2", "20.00")):
        m.add_attachment(_pdf(no, total), maintype="application", subtype="pdf", filename=f"{no}.pdf")
    (input_root / "loose.eml").write_bytes(bytes(m))
    e = client.post("/extract", json={"file_location": "loose.eml", "extended": True}).json()
    assert len(e["data"]) == 2 and "unplaced_content" in e["review_reasons"]
    assert e["status"] == "review"


def test_single_record_is_still_an_array(client):
    data = client.post("/extract", json={"file_location": "invoice.pdf"}).json()
    assert isinstance(data, list) and len(data) == 1


def test_model_path_returns_several_records_and_invented_ones_fail(settings):
    real = _record()
    invented = _record(invoice_number={"value": "INV-99999", "source": "d2#Invoice!A3", "confidence": 0.9},
                       total_amount={"value": "77777", "source": "d2#Invoice!D13", "confidence": 0.9})
    gw = FakeGateway({"records": [real, invented]})
    e = TestClient(create_app(settings, model_gateway=gw)).post(
        "/extract", json={"file_location": "invoice-email.eml", "client": "acme", "extended": True}).json()
    assert len(e["records"]) == 2
    assert e["records"][0]["data"]["invoice_number"] == "INV-20194"
    bad = e["records"][1]
    assert bad["data"]["invoice_number"] is None and bad["data"]["total_amount"] is None
    assert "unverified_value:invoice_number" in bad["review_reasons"] and bad["status"] == "review"
    assert e["status"] == "review"


def test_record_key_must_name_a_scalar_field():
    base = {"fields": [{"name": "a", "type": "string"},
                       {"name": "rows", "type": "array", "items": [{"name": "x", "type": "string"}]}]}
    assert load_dictionary(base).record_key == "a"
    with pytest.raises(ConfigError):
        load_dictionary({**base, "record_key": "rows"})
    with pytest.raises(ConfigError):
        load_dictionary({**base, "record_key": "missing"})


def test_config_declares_its_record_key(config_root):
    assert ConfigStore(config_root).resolve().dictionary.record_key == "invoice_number"
