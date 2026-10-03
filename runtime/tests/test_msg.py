"""Outlook .msg input: same pipeline and same results as .eml."""
import io
import zipfile
from email.message import EmailMessage

import compressed_rtf
import pytest

from extractor_service.config_store import DEFAULT_INTAKE
from extractor_service.intake import detect_kind, open_submission
from extractor_service.msg import read_msg, rtf_to_text
from tests.conftest import data_of
from tests.msg_writer import build_msg
from tests.samples import invoice_msg, invoice_xlsx, statement_csv

BS = b"\x5c"


def _w(x: bytes) -> bytes:
    return BS + x


PLAIN_RTF = (b"{" + _w(b"rtf1") + _w(b"ansi") + _w(b"ansicpg1252") + _w(b"deff0") + b"{" + _w(b"fonttbl")
             + b"{" + _w(b"f0 Calibri;") + b"}}" + _w(b"f0 Invoice No: INV-77") + _w(b"par Invoice Date: 2026-07-01")
             + _w(b"par Supplier: Caf") + _w(b"'e9 Lumi") + _w(b"'e8re") + _w(b"par Total Due: ") + _w(b"'a3450.00")
             + _w(b"par") + b"}")
HTML_RTF = (b"{" + _w(b"rtf1") + _w(b"ansi") + _w(b"ansicpg1252") + _w(b"fromhtml1 ") + _w(b"deff0") + b"{"
            + _w(b"fonttbl") + b"{" + _w(b"f0") + _w(b"fswiss Arial;") + b"}}" + b"{" + _w(b"*") + _w(b"htmltag19 <html>")
            + b"}" + b"{" + _w(b"*") + _w(b"htmltag64 <p>") + b"}" + _w(b"htmlrtf ") + b"{" + _w(b"htmlrtf0 ")
            + b"Invoice No: INV-88" + _w(b"htmlrtf") + _w(b"par") + b"}" + _w(b"htmlrtf0 ") + b"{" + _w(b"*")
            + _w(b"htmltag72 </p>") + b"}" + b"{" + _w(b"*") + _w(b"htmltag64 <p>") + b"}" + _w(b"htmlrtf ") + b"{"
            + _w(b"htmlrtf0 ") + b"Total Due: $99.50" + _w(b"htmlrtf") + _w(b"par") + b"}" + _w(b"htmlrtf0 ") + b"{"
            + _w(b"*") + _w(b"htmltag72 </p>") + b"}}")


def test_msg_and_eml_give_the_same_extraction(client):
    eml = client.post("/extract", json={"file_location": "invoice-email.eml"})
    msg = client.post("/extract", json={"file_location": "invoice-email.msg"})
    assert msg.status_code == 200 and msg.json() == eml.json()
    assert msg.headers["x-extraction-status"] == "extracted"


def test_msg_metadata(client):
    e = client.post("/extract", json={"file_location": "invoice-email.msg", "extended": True}).json()
    meta = e["metadata"]
    assert meta["input"]["kind"] == "msg"
    assert meta["input"]["subject"] == "Invoice INV-20194 from Acme Corp"
    assert meta["input"]["sender"] == "Acme billing <billing@acme.example>"
    assert [d["source"] for d in meta["documents"]] == ["body", "attachment:INV-20194.xlsx"]
    assert meta["skipped"] == [{"item": "logo.png", "reason": "image_not_supported"}]
    assert e["records"][0]["fields"]["total_amount"]["source"] == "attachment:INV-20194.xlsx#Invoice!D13"


def test_kind_is_detected_from_bytes_whatever_the_name(client, input_root):
    (input_root / "mail.dat").write_bytes(invoice_msg())
    assert detect_kind(invoice_msg()) == "msg"
    assert client.post("/extract", json={"file_location": "mail.dat"}).json()[0]["invoice_number"] == "INV-20194"


def test_rtf_only_body_is_read(client, input_root):
    (input_root / "rtf.msg").write_bytes(build_msg(subject="Invoice", sender_name="Café Lumière",
                                                   rtf_compressed=compressed_rtf.compress(PLAIN_RTF, compressed=True)))
    [body] = data_of(client.post("/extract", json={"file_location": "rtf.msg"}))
    assert body["invoice_number"] == "INV-77" and body["invoice_date"] == "2026-07-01"
    assert body["vendor"] == "Café Lumière" and body["total_amount"] == 450


def test_html_in_rtf_keeps_line_breaks():
    assert rtf_to_text(HTML_RTF) == "Invoice No: INV-88\nTotal Due: $99.50"


def test_html_body_when_no_plain_body():
    m = read_msg(build_msg(subject="x", html=b'<html><head><meta charset="utf-8"></head><body>'
                                             b"<p>Invoice No: INV-99</p><p>Total Due: \xe2\x82\xac7.00</p></body></html>"))
    assert m.body_text.splitlines() == ["Invoice No: INV-99", "Total Due: €7.00"]


def test_ansi_strings_use_the_message_code_page():
    m = read_msg(build_msg(subject="Réf facture", sender_name="Société Générale",
                           body="Montant dû: 1.234,56 €", ansi_codepage=1252))
    assert (m.subject, m.sender, m.body_text) == ("Réf facture", "Société Générale", "Montant dû: 1.234,56 €")


def test_attached_outlook_item_is_read_recursively():
    inner = build_msg(subject="older thread", body="Invoice No: INV-5\r\nTotal Due: $5.00", embedded=True,
                      attachments=[{"name": "inner.csv", "data": statement_csv()}])
    data = build_msg(subject="Fwd: statement", body="see attached",
                     attachments=[{"name": "Original message", "msg": inner},
                                  {"name": "statement.csv", "data": statement_csv()}])
    sub = open_submission("f.msg", "f.msg", data, DEFAULT_INTAKE)
    assert [(i.kind, i.source_prefix) for i in sub.items] == [
        ("email_body", "body"), ("email_body", "embedded:1:Original message"),
        ("csv", "embedded:1:Original message/inner.csv"), ("csv", "attachment:statement.csv")]
    assert sub.items[1].meta["subject"] == "older thread" and sub.reasons == []


def test_msg_inside_eml_and_zip_is_read_as_an_attached_email():
    eml = EmailMessage()
    eml["Subject"] = "Fwd"
    eml.set_content("forwarding")
    eml.add_attachment(invoice_msg(), maintype="application", subtype="vnd.ms-outlook", filename="orig.msg")
    sub = open_submission("e.eml", "e.eml", bytes(eml), DEFAULT_INTAKE)
    assert [i.source_prefix for i in sub.items] == ["body", "embedded:1:orig.msg",
                                                    "embedded:1:orig.msg/INV-20194.xlsx"]
    assert {"item": "orig.msg/logo.png", "reason": "image_not_supported"} in sub.skipped
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("orig.msg", invoice_msg())
    sub = open_submission("z.zip", "z.zip", buf.getvalue(), DEFAULT_INTAKE)
    assert [i.source_prefix for i in sub.items] == ["embedded:1:orig.msg", "embedded:1:orig.msg/INV-20194.xlsx"]


def test_zip_attached_to_msg_is_unpacked():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("invoice.xlsx", invoice_xlsx())
    sub = open_submission("m.msg", "m.msg", build_msg(subject="Invoices", body="attached",
                                                      attachments=[{"name": "batch.zip", "data": buf.getvalue()}]),
                          DEFAULT_INTAKE)
    assert [i.source_prefix for i in sub.items] == ["body", "attachment:batch.zip/invoice.xlsx"]


def test_duplicate_attachment_names_kept_apart():
    sub = open_submission("d.msg", "d.msg", build_msg(subject="two", body="x", attachments=[
        {"name": "s.csv", "data": statement_csv()}, {"name": "s.csv", "data": statement_csv()}]), DEFAULT_INTAKE)
    assert [i.name for i in sub.items if i.kind != "email_body"] == ["s.csv", "s (2).csv"]


@pytest.mark.parametrize("cut", [600, 2000])
def test_damaged_msg_is_still_a_result_flagged_with_the_error(client, input_root, cut):
    (input_root / "bad.msg").write_bytes(invoice_msg()[:cut])
    r = client.post("/extract", json={"file_location": "bad.msg"})
    body = r.json()
    assert r.status_code == 200 and r.headers["x-extraction-status"] == "extracted"
    assert body["flagged"] is True and body["data"] == []
    assert body["flags"][0] in ("error:malformed_email", "error:unsupported_input:ole2")
