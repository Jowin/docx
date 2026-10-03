import json

import pytest

from tests.tools.conftest import encrypt_pdf
from extractor_tools import ToolError, extract_pdf_text


def test_text_lines_tables_and_locators(invoice_pdf):
    r = extract_pdf_text(invoice_pdf, "invoice.pdf")["result"]
    assert r["page_count"] == 2
    p1 = r["pages"][0]
    assert p1["has_text_layer"] and "Invoice INV-20194" in p1["text"]
    assert p1["lines"][0]["locator"] == "p1:L1"
    cells = {c["locator"]: c["text"] for row in p1["tables"][0]["rows"] for c in row}
    assert cells["p1:T1:R3C2"] == "$12,400.00"


def test_scanned_page_measurements_for_ocr_decision(invoice_pdf):
    r = extract_pdf_text(invoice_pdf)["result"]
    p2 = r["pages"][1]
    assert r["pages_without_text_layer"] == [2]
    assert p2["char_count"] == 0 and p2["image_count"] == 1 and p2["image_coverage"] > 0.4


def test_owner_password_only_is_readable(invoice_pdf):
    r = extract_pdf_text(encrypt_pdf(invoice_pdf, "", "owner"))["result"]
    assert r["encrypted"] and r["owner_password_only"]
    assert "INV-20194" in r["pages"][0]["text"]


def test_user_password_is_typed_failure(invoice_pdf):
    with pytest.raises(ToolError) as e:
        extract_pdf_text(encrypt_pdf(invoice_pdf, "secret", "owner"))
    assert e.value.code == "file_encrypted" and e.value.permanent


def test_not_a_pdf():
    with pytest.raises(ToolError) as e:
        extract_pdf_text(b"Invoice,Amount\n")
    assert e.value.code == "format_mismatch"


def test_page_limit(invoice_pdf):
    with pytest.raises(ToolError) as e:
        extract_pdf_text(invoice_pdf, max_pages=1)
    assert e.value.code == "input_too_large"


def test_deterministic(invoice_pdf):
    a = json.dumps(extract_pdf_text(invoice_pdf), sort_keys=True)
    assert a == json.dumps(extract_pdf_text(invoice_pdf), sort_keys=True)
